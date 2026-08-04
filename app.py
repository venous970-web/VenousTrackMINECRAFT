import base64
import concurrent.futures
import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, render_template, request, session
from flask_cors import CORS
from mcstatus import JavaServer
import requests

app = Flask(__name__)
CORS(app, supports_credentials=True)

# Chiave segreta per le sessioni di login
app.secret_key = os.environ.get("SECRET_KEY", "chiave-super-segreta-venous970")

# Database separati: il generale usa il vecchio db storico, l'italiano riparte da zero
DB_ITA = "venous_track_ita.db"
DB_GEN = "venous_track.db" 
CONFIG_FILE = "servers.json"
USERS_FILE = "users.json"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "metti_qui_la_password_provvisoria")

SERVER_STATES = {}
MAX_FAILS = 3
PING_INTERVAL = 5
ITALY_TZ = ZoneInfo("Europe/Rome")

# --- GESTIONE UTENTI E PERMESSI ---

def init_users_file():
    if not os.path.exists(USERS_FILE):
        default_users = {
            "Venous970": {
                "add_italiano": True,
                "add_generale": True,
                "delete_server": True
            },
            "guest": {
                "add_italiano": False,
                "add_generale": True,
                "delete_server": False
            }
        }
        with open(USERS_FILE, "w") as f:
            json.dump(default_users, f, indent=4)

init_users_file()

def get_permissions(username):
    try:
        with open(USERS_FILE, "r") as f:
            users = json.load(f)
            return users.get(username, users.get("guest"))
    except Exception:
        return {"add_italiano": False, "add_generale": True, "delete_server": False}

# --- INIZIALIZZAZIONE DATABASE ---

def init_db(db_file):
    conn = sqlite3.connect(db_file)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS server_stats (
            ip TEXT,
            timestamp INTEGER,
            players INTEGER,
            max_players INTEGER
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_ip_time ON server_stats(ip, timestamp)")
    conn.commit()
    conn.close()

init_db(DB_ITA)
init_db(DB_GEN)

# --- GESTIONE GITHUB E SERVER ---

def load_servers_from_github():
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/servers.json"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    try:
        res = requests.get(url, headers=headers)
        if res.status_code == 200:
            content_b64 = res.json().get("content")
            if content_b64:
                decoded = base64.b64decode(content_b64).decode("utf-8")
                data = json.loads(decoded)
                if isinstance(data, list):
                    return {"italiani": [], "generali": data}
                return data
    except Exception as e:
        print("Errore caricamento da GitHub:", e)
    return None

def save_servers_to_github(servers_data):
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/servers.json"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    sha = None
    try:
        res = requests.get(url, headers=headers)
        if res.status_code == 200:
            sha = res.json().get("sha")
    except Exception:
        pass

    json_str = json.dumps(servers_data, indent=4)
    content_b64 = base64.b64encode(json_str.encode("utf-8")).decode("utf-8")
    payload = {
        "message": "Auto-update servers.json from Render app",
        "content": content_b64,
    }
    if sha:
        payload["sha"] = sha
    try:
        requests.put(url, headers=headers, json=payload)
    except Exception as e:
        print("Errore salvataggio su GitHub:", e)

def load_servers():
    gh_servers = load_servers_from_github()
    if gh_servers and isinstance(gh_servers, dict):
        with open(CONFIG_FILE, "w") as f:
            json.dump(gh_servers, f, indent=4)
        return gh_servers

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
            
    default_servers = {
        "italiani": ["venous.coralmc.it", "play.metamc.it", "play.tecnocraft.net", "play.scarletmc.it"],
        "generali": ["hypixel.net", "donutsmp.net"]
    }
    save_servers(default_servers)
    return default_servers

def save_servers(servers):
    with open(CONFIG_FILE, "w") as f:
        json.dump(servers, f, indent=4)
    save_servers_to_github(servers)

servers_data = load_servers()

# --- TRACKER DI BACKGROUND ---

def query_minecraft_server(ip):
    if ":" in ip:
        try:
            host, port_str = ip.split(":", 1)
            try:
                server = JavaServer(host, int(port_str), timeout=4.0)
            except TypeError:
                server = JavaServer(host, int(port_str))
            return server.status(), None
        except Exception as e:
            return None, f"Porta diretta: {e}"
    try:
        try:
            server = JavaServer.lookup(ip, timeout=4.0)
        except TypeError:
            server = JavaServer.lookup(ip)
        return server.status(), None
    except Exception as e_srv:
        try:
            try:
                server = JavaServer(ip, 25565, timeout=4.0)
            except TypeError:
                server = JavaServer(ip, 25565)
            return server.status(), None
        except Exception as e_dir:
            return None, f"SRV fallito ({e_srv}) | Diretto fallito ({e_dir})"

def background_tracker():
    while True:
        now_ts = int(time.time())
        all_servers = []
        for cat in ["italiani", "generali"]:
            for ip in servers_data.get(cat, []):
                all_servers.append((ip, cat))

        if all_servers:
            time_str = datetime.now(ITALY_TZ).strftime("%H:%M:%S")
            print(f"\n🔄 [{time_str}] Avvio ping PARALLELO per {len(all_servers)} server...")
            start_t = time.time()
            ping_results = []
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(all_servers), 1)) as executor:
                futures = {executor.submit(query_minecraft_server, item[0]): item for item in all_servers}
                for future in concurrent.futures.as_completed(futures):
                    ip, cat = futures[future]
                    status, err = future.result()
                    ping_results.append((ip, cat, status, err))

            elapsed = round(time.time() - start_t, 2)
            print(f"⏱️ Controllati {len(all_servers)} server in simultanea ({elapsed}s)")

            conn_ita = sqlite3.connect(DB_ITA)
            conn_gen = sqlite3.connect(DB_GEN)
            c_ita = conn_ita.cursor()
            c_gen = conn_gen.cursor()

            for ip, cat, status, err in ping_results:
                if ip not in SERVER_STATES:
                    SERVER_STATES[ip] = {
                        "ip": ip, "online": True, "players": 0, "max": 0,
                        "version": "Inizializzazione...", "favicon": None, "fail_count": 0, "category": cat
                    }

                state = SERVER_STATES[ip]
                target_cursor = c_ita if cat == "italiani" else c_gen

                if status is not None:
                    players = getattr(status.players, "online", 0)
                    max_p = getattr(status.players, "max", 0)
                    version = status.version.name if hasattr(status, "version") and hasattr(status.version, "name") else "1.7.x - 1.21.x"
                    favicon = getattr(status, "favicon", getattr(status, "icon", None))

                    state.update({"fail_count": 0, "online": True, "players": players, "max": max_p, "version": version})
                    if favicon: state["favicon"] = favicon

                    target_cursor.execute("INSERT INTO server_stats VALUES (?, ?, ?, ?)", (ip, now_ts, players, max_p))
                    print(f"  🟢 {ip} ({cat}) -> ONLINE | Gioc: {players}/{max_p}")
                else:
                    state["fail_count"] += 1
                    if state["fail_count"] >= MAX_FAILS or not state["online"]:
                        state.update({"online": False, "players": 0, "version": "Non raggiungibile"})
                    print(f"  🔴 {ip} ({cat}) -> OFFLINE ({state['fail_count']}/{MAX_FAILS}) | Err: {err}")

            # FONDAMENTALE: Esegue il commit e chiude le connessioni per salvare effettivamente su disco .db
            conn_ita.commit()
            conn_ita.close()
            conn_gen.commit()
            conn_gen.close()

        time.sleep(PING_INTERVAL)

tracking_thread = threading.Thread(target=background_tracker, daemon=True)
tracking_thread.start()

# --- ANALYTICS ---

def safe_int_format(val):
    if val is None: return "-"
    try: return f"{int(val):,}".replace(",", ".")
    except (ValueError, TypeError): return str(val)

def get_server_analytics(ip, current_players, category):
    db_file = DB_ITA if category == "italiani" else DB_GEN
    now_ts = int(time.time())
    conn = sqlite3.connect(db_file)
    c = conn.cursor()

    ts_72h = now_ts - (72 * 3600)
    c.execute("SELECT MAX(players) FROM server_stats WHERE ip = ? AND timestamp >= ?", (ip, ts_72h))
    row_72h = c.fetchone()
    peak_val = row_72h[0] if row_72h and row_72h[0] is not None else current_players

    c.execute("SELECT players, timestamp FROM server_stats WHERE ip = ? ORDER BY players DESC LIMIT 1", (ip,))
    row_rec = c.fetchone()
    if row_rec and row_rec[0] is not None:
        rec_players = row_rec[0]
        try: rec_date = datetime.fromtimestamp(int(row_rec[1]), ITALY_TZ).strftime("%d/%m/%Y")
        except: rec_date = datetime.now(ITALY_TZ).strftime("%d/%m/%Y")
    else:
        rec_players = current_players
        rec_date = datetime.now(ITALY_TZ).strftime("%d/%m/%Y")

    def get_past_players(seconds_ago):
        target_ts = now_ts - seconds_ago
        min_ts, max_ts = target_ts - 10800, target_ts + 10800
        c.execute("SELECT players FROM server_stats WHERE ip = ? AND timestamp BETWEEN ? AND ? ORDER BY ABS(timestamp - ?) ASC LIMIT 1", (ip, min_ts, max_ts, target_ts))
        r = c.fetchone()
        return safe_int_format(r[0]) if r and r[0] is not None else "-"

    p_1d, p_2d, p_3d = get_past_players(86400), get_past_players(2 * 86400), get_past_players(3 * 86400)
    
    c.execute("SELECT timestamp, players FROM server_stats WHERE ip = ? ORDER BY timestamp DESC LIMIT 25", (ip,))
    rows_chart = c.fetchall()
    conn.close()

    rows_chart.reverse()
    chart_labels = [datetime.fromtimestamp(r[0], ITALY_TZ).strftime("%H:%M:%S") for r in rows_chart]
    chart_data = [r[1] for r in rows_chart]

    if len(chart_data) < 2:
        chart_labels = ["--:--:--", datetime.now(ITALY_TZ).strftime("%H:%M:%S")]
        chart_data = [current_players, current_players]

    return {
        "peak_72h": safe_int_format(peak_val), "record": f"{safe_int_format(rec_players)} ({rec_date})",
        "day_1": p_1d, "day_2": p_2d, "day_3": p_3d, "chart_labels": chart_labels, "chart_data": chart_data,
    }

# --- ROTTE API ---

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/auth", methods=["GET"])
def check_auth():
    username = session.get("username", "guest")
    perms = get_permissions(username)
    return jsonify({"logged_in": username != "guest", "username": username, "permissions": perms})

@app.route("/api/login", methods=["POST"])
def login():
    data = request.json or {}
    username = data.get("username")
    password = data.get("password")
    
    if username == "Venous970" and password == ADMIN_PASSWORD:
        session["username"] = username
        return jsonify({"status": "success", "message": "Login effettuato", "permissions": get_permissions(username)})
    return jsonify({"status": "error", "message": "Credenziali errate"}), 401

@app.route("/api/logout", methods=["POST"])
def logout():
    session.pop("username", None)
    return jsonify({"status": "success", "message": "Logout effettuato"})

@app.route("/api/servers/<category>", methods=["GET", "POST", "DELETE"])
def handle_servers(category):
    global servers_data
    if category not in ["italiani", "generali"]:
        return jsonify({"status": "error", "message": "Categoria non valida"}), 400

    username = session.get("username", "guest")
    perms = get_permissions(username)

    if request.method == "POST":
        if (category == "italiani" and not perms["add_italiano"]) or (category == "generali" and not perms["add_generale"]):
            return jsonify({"status": "error", "message": "Non hai i permessi per aggiungere in questa categoria"}), 403

        data = request.json or {}
        ip = data.get("ip", "").strip()
        
        if ip and ip not in servers_data[category]:
            other_cat = "generali" if category == "italiani" else "italiani"
            if ip in servers_data[other_cat]:
                servers_data[other_cat].remove(ip)
                
            servers_data[category].append(ip)
            save_servers(servers_data)
            
            status, _ = query_minecraft_server(ip)
            if status is not None:
                players = getattr(status.players, "online", 0)
                max_p = getattr(status.players, "max", 0)
                SERVER_STATES[ip] = {
                    "ip": ip, "online": True, "players": players, "max": max_p,
                    "version": status.version.name if hasattr(status, "version") and hasattr(status.version, "name") else "1.7.x - 1.21.x",
                    "favicon": getattr(status, "favicon", getattr(status, "icon", None)),
                    "fail_count": 0, "category": category
                }
                db_file = DB_ITA if category == "italiani" else DB_GEN
                conn = sqlite3.connect(db_file)
                c = conn.cursor()
                c.execute("INSERT INTO server_stats VALUES (?, ?, ?, ?)", (ip, int(time.time()), players, max_p))
                conn.commit()
                conn.close()

            return jsonify({"status": "success", "servers": servers_data[category]})
        return jsonify({"status": "error", "message": "IP non valido o già presente"}), 400
    
    elif request.method == "DELETE":
        if not perms["delete_server"]:
            return jsonify({"status": "error", "message": "Non hai i permessi per eliminare i server"}), 403

        data = request.json or {}
        ip = data.get("ip", "").strip()
        if ip in servers_data[category]:
            servers_data[category].remove(ip)
            save_servers(servers_data)
            if ip in SERVER_STATES:
                del SERVER_STATES[ip]
            return jsonify({"status": "success", "servers": servers_data[category]})
        return jsonify({"status": "error", "message": "IP non trovato nella lista"}), 404

    return jsonify(servers_data[category])

@app.route("/api/stats/<category>", methods=["GET"])
def get_stats(category):
    if category not in ["italiani", "generali"]:
        return jsonify({"status": "error", "message": "Categoria non valida"}), 400

    full_results = []
    for ip in servers_data.get(category, []):
        state = SERVER_STATES.get(ip, {
            "ip": ip, "online": True, "players": 0, "max": 0,
            "version": "Inizializzazione...", "favicon": None, "category": category
        }).copy()

        analytics = get_server_analytics(ip, state["players"], category)
        state.update(analytics)
        full_results.append(state)

    full_results.sort(key=lambda x: (x["online"], x["players"]), reverse=True)
    return jsonify(full_results)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
