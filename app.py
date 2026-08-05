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

app.secret_key = os.environ.get("SECRET_KEY", "chiave-super-segreta-venous970")

# --- PERCORSI ASSOLUTI ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

DB_ITA = os.path.join(BASE_DIR, "venous_track_ita.db")
DB_GEN = os.path.join(BASE_DIR, "venous_track.db") 
CONFIG_FILE = os.path.join(BASE_DIR, "servers.json")
USERS_FILE = os.path.join(BASE_DIR, "users.json")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "metti_qui_la_password_provvisoria")

SERVER_STATES = {}
MAX_FAILS = 3
PING_INTERVAL = 5
ITALY_TZ = ZoneInfo("Europe/Rome")

# --- SINCRONIZZAZIONE GITHUB (DB E JSON) ---
def download_file_from_github(filepath, filename):
    if not GITHUB_TOKEN or not GITHUB_REPO:
        print(f"⚠️ Salto download di {filename}: Token o Repo non configurati.")
        return
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    try:
        res = requests.get(url, headers=headers)
        if res.status_code == 200:
            content_b64 = res.json().get("content")
            if content_b64:
                with open(filepath, "wb") as f:
                    f.write(base64.b64decode(content_b64))
                print(f"📥 {filename} scaricato da GitHub con successo.")
        elif res.status_code == 404:
            print(f"ℹ️ {filename} non esiste ancora su GitHub. Verrà creato al primo backup.")
        else:
            print(f"⚠️ Errore download {filename}: {res.status_code} - {res.text}")
    except Exception as e:
        print(f"⚠️ Eccezione durante il download di {filename}: {e}")

def sync_file_to_github(filepath, filename):
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    
    sha = None
    try:
        res = requests.get(url, headers=headers)
        if res.status_code == 200:
            sha = res.json().get("sha")
    except:
        pass

    try:
        if os.path.exists(filepath):
            with open(filepath, "rb") as f:
                content_b64 = base64.b64encode(f.read()).decode("utf-8")
                
            payload = {
                "message": f"Auto-backup {filename} da app VenousTrack",
                "content": content_b64,
            }
            if sha:
                payload["sha"] = sha
                
            res = requests.put(url, headers=headers, json=payload)
            if res.status_code in [200, 201]:
                print(f"✅ Backup di {filename} su GitHub completato.")
            else:
                print(f"❌ Errore GitHub upload {filename}: {res.status_code} - {res.text}")
    except Exception as e:
        print(f"❌ Eccezione durante l'upload di {filename}: {e}")

def hourly_github_backup():
    while True:
        # Aspetta 60 minuti prima di fare il backup
        time.sleep(3600)  
        print("\n⏳ Avvio backup orario dei file su GitHub...")
        sync_file_to_github(DB_ITA, "venous_track_ita.db")
        sync_file_to_github(DB_GEN, "venous_track.db")
        sync_file_to_github(CONFIG_FILE, "servers.json")

# --- DOWNLOAD INIZIALE DEI DATI AL RIAVVIO DI RENDER ---
print("🔄 Esecuzione download iniziale dei database da GitHub...")
download_file_from_github(DB_ITA, "venous_track_ita.db")
download_file_from_github(DB_GEN, "venous_track.db")
download_file_from_github(CONFIG_FILE, "servers.json")

# Avvia il thread di backup orario in background
backup_thread = threading.Thread(target=hourly_github_backup, daemon=True)
backup_thread.start()

# --- GESTIONE UTENTI E PERMESSI ---
def init_users_file():
    if not os.path.exists(USERS_FILE):
        default_users = {
            "Venous970": {"add_italiano": True, "add_generale": True, "delete_server": True},
            "guest": {"add_italiano": False, "add_generale": True, "delete_server": False}
        }
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(default_users, f, indent=4)

init_users_file()

def get_permissions(username):
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
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

# --- CARICAMENTO SERVER ---
def load_servers():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
            
    default_servers = {
        "italiani": ["venous.coralmc.it", "play.metamc.it", "play.tecnocraft.net", "play.scarletmc.it"],
        "generali": ["hypixel.net", "donutsmp.net"]
    }
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(default_servers, f, indent=4)
    return default_servers

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
        all_servers = [(ip, cat) for cat in ["italiani", "generali"] for ip in servers_data.get(cat, [])]

        if all_servers:
            time_str = datetime.now(ITALY_TZ).strftime("%H:%M:%S")
            print(f"\n🔄 [{time_str}] Avvio ping per {len(all_servers)} server...")
            start_t = time.time()
            ping_results = []
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(all_servers), 1)) as executor:
                futures = {executor.submit(query_minecraft_server, item[0]): item for item in all_servers}
                for future in concurrent.futures.as_completed(futures):
                    ip, cat = futures[future]
                    status, err = future.result()
                    ping_results.append((ip, cat, status, err))

            conn_ita = sqlite3.connect(DB_ITA)
            conn_gen = sqlite3.connect(DB_GEN)
            c_ita = conn_ita.cursor()
            c_gen = conn_gen.cursor()

            try:
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
                    else:
                        state["fail_count"] += 1
                        if state["fail_count"] >= MAX_FAILS or not state["online"]:
                            state.update({"online": False, "players": 0, "version": "Non raggiungibile"})

                conn_ita.commit()
                conn_gen.commit()
            except Exception as db_err:
                print("❌ Errore tracker:", db_err)
            finally:
                conn_ita.close()
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
    try:
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
    finally:
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
            return jsonify({"status": "error", "message": "Non hai i permessi per aggiungere"}), 403

        data = request.json or {}
        ip = data.get("ip", "").strip()
        
        if ip and ip not in servers_data[category]:
            other_cat = "generali" if category == "italiani" else "italiani"
            if ip in servers_data[other_cat]:
                servers_data[other_cat].remove(ip)
                
            servers_data[category].append(ip)
            sync_file_to_github(CONFIG_FILE, "servers.json") 
            
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
                try:
                    c = conn.cursor()
                    c.execute("INSERT INTO server_stats VALUES (?, ?, ?, ?)", (ip, int(time.time()), players, max_p))
                    conn.commit()
                finally:
                    conn.close()

            return jsonify({"status": "success", "servers": servers_data[category]})
        return jsonify({"status": "error", "message": "IP non valido o già presente"}), 400
    
    elif request.method == "DELETE":
        if not perms["delete_server"]:
            return jsonify({"status": "error", "message": "Non hai i permessi"}), 403

        data = request.json or {}
        ip = data.get("ip", "").strip()
        if ip in servers_data[category]:
            servers_data[category].remove(ip)
            sync_file_to_github(CONFIG_FILE, "servers.json")
            if ip in SERVER_STATES:
                del SERVER_STATES[ip]
            return jsonify({"status": "success", "servers": servers_data[category]})
        return jsonify({"status": "error", "message": "IP non trovato"}), 404

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
