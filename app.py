import concurrent.futures
import json
import os
import sqlite3
import time
import threading
from datetime import datetime
from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
from mcstatus import JavaServer

app = Flask(__name__)
CORS(app)

DB_FILE = "venous_track.db"
CONFIG_FILE = "servers.json"

SERVER_STATES = {}
MAX_FAILS = 3
PING_INTERVAL = 5  # Intervallo del ping in secondi

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS server_stats (
            ip TEXT,
            timestamp INTEGER,
            players INTEGER,
            max_players INTEGER
        )
    ''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_ip_time ON server_stats(ip, timestamp)')
    conn.commit()
    conn.close()

init_db()

def load_servers():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    default_servers = [
        "mc.coralmc.it",
        "play.metamc.it",
        "play.tecnocraft.net",
        "play.fruitmc.it",
        "play.scarletmc.it"
    ]
    save_servers(default_servers)
    return default_servers

def save_servers(servers):
    with open(CONFIG_FILE, "w") as f:
        json.dump(servers, f, indent=4)

servers_list = load_servers()

def query_minecraft_server(ip):
    """ Pinga un singolo server gestendo il timeout in fase di inizializzazione """
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
    """ Thread in background che esegue il ping SIMULTANEO di tutti i server """
    while True:
        now_ts = int(time.time())
        current_servers = list(servers_list)
        
        if current_servers:
            time_str = datetime.now().strftime('%H:%M:%S')
            print(f"\n🔄 [{time_str}] Avvio ping PARALLELO per {len(current_servers)} server...")
            start_t = time.time()
            
            ping_results = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(current_servers), 1)) as executor:
                futures = {executor.submit(query_minecraft_server, ip): ip for ip in current_servers}
                for future in concurrent.futures.as_completed(futures):
                    ip = futures[future]
                    status, err = future.result()
                    ping_results.append((ip, status, err))

            elapsed = round(time.time() - start_t, 2)
            print(f"⏱️ Controllati {len(current_servers)} server in simultanea ({elapsed}s)")
            
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            
            for ip, status, err in ping_results:
                if ip not in SERVER_STATES:
                    SERVER_STATES[ip] = {
                        "ip": ip,
                        "online": True,
                        "players": 0,
                        "max": 0,
                        "version": "Inizializzazione...",
                        "favicon": None,
                        "fail_count": 0
                    }
                    
                state = SERVER_STATES[ip]
                
                if status is not None:
                    players = getattr(status.players, 'online', 0)
                    max_p = getattr(status.players, 'max', 0)
                    version = status.version.name if hasattr(status, 'version') and hasattr(status.version, 'name') else "1.7.x - 1.21.x"
                    favicon = getattr(status, 'favicon', getattr(status, 'icon', None))
                    
                    state["fail_count"] = 0
                    state["online"] = True
                    state["players"] = players
                    state["max"] = max_p
                    state["version"] = version
                    if favicon:
                        state["favicon"] = favicon
                        
                    c.execute("INSERT INTO server_stats VALUES (?, ?, ?, ?)", (ip, now_ts, players, max_p))
                    print(f"  🟢 {ip} -> ONLINE | Giocatori: {players}/{max_p}")
                else:
                    state["fail_count"] += 1
                    if state["fail_count"] >= MAX_FAILS or not state["online"]:
                        state["online"] = False
                        state["players"] = 0
                        state["version"] = "Non raggiungibile"
                    print(f"  🔴 {ip} -> OFFLINE ({state['fail_count']}/{MAX_FAILS}) | Errore: {err}")

            conn.commit()
            conn.close()

        time.sleep(PING_INTERVAL)

tracking_thread = threading.Thread(target=background_tracker, daemon=True)
tracking_thread.start()

def safe_int_format(val):
    if val is None:
        return "-"
    try:
        return f"{int(val):,}".replace(",", ".")
    except (ValueError, TypeError):
        return str(val)

def get_server_analytics(ip, current_players):
    now_ts = int(time.time())
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    ts_72h = now_ts - (72 * 3600)
    c.execute("SELECT MAX(players) FROM server_stats WHERE ip = ? AND timestamp >= ?", (ip, ts_72h))
    row_72h = c.fetchone()
    peak_val = row_72h[0] if row_72h and row_72h[0] is not None else current_players
    
    c.execute("SELECT players, timestamp FROM server_stats WHERE ip = ? ORDER BY players DESC LIMIT 1", (ip,))
    row_rec = c.fetchone()
    if row_rec and row_rec[0] is not None:
        rec_players = row_rec[0]
        try:
            rec_date = datetime.fromtimestamp(int(row_rec[1])).strftime("%d/%m/%Y")
        except (ValueError, TypeError, OSError):
            rec_date = datetime.now().strftime("%d/%m/%Y")
    else:
        rec_players = current_players
        rec_date = datetime.now().strftime("%d/%m/%Y")
        
    def get_past_players(seconds_ago):
        target_ts = now_ts - seconds_ago
        # Finestra di tolleranza di 3 ore (10800 secondi) attorno al momento esatto nel passato
        # In questo modo ignoriamo i timestamp modificati/antichi come quello del record nel 2023
        min_ts = target_ts - 10800
        max_ts = target_ts + 10800
        
        c.execute("SELECT players FROM server_stats WHERE ip = ? AND timestamp BETWEEN ? AND ? ORDER BY ABS(timestamp - ?) ASC LIMIT 1", (ip, min_ts, max_ts, target_ts))
        r = c.fetchone()
        if r and r[0] is not None:
            return safe_int_format(r[0])
        return "-"
        
    p_1d = get_past_players(86400)
    p_2d = get_past_players(2 * 86400)
    p_3d = get_past_players(3 * 86400)
    
    c.execute("SELECT timestamp, players FROM server_stats WHERE ip = ? ORDER BY timestamp DESC LIMIT 25", (ip,))
    rows_chart = c.fetchall()
    conn.close()
    
    rows_chart.reverse()
    chart_labels = [datetime.fromtimestamp(r[0]).strftime("%H:%M:%S") for r in rows_chart]
    chart_data = [r[1] for r in rows_chart]
    
    if len(chart_data) < 2:
        chart_labels = ["--:--:--", datetime.now().strftime("%H:%M:%S")]
        chart_data = [current_players, current_players]
        
    return {
        "peak_72h": safe_int_format(peak_val),
        "record": f"{safe_int_format(rec_players)} ({rec_date})",
        "day_1": p_1d,
        "day_2": p_2d,
        "day_3": p_3d,
        "chart_labels": chart_labels,
        "chart_data": chart_data
    }

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/servers", methods=["GET", "POST", "DELETE"])
def handle_servers():
    global servers_list
    if request.method == "POST":
        data = request.get_json() or {}
        new_ip = data.get("ip", "").strip()
        if new_ip and new_ip not in servers_list:
            servers_list.append(new_ip)
            save_servers(servers_list)
            SERVER_STATES.pop(new_ip, None)
        return jsonify(servers_list)
    elif request.method == "DELETE":
        data = request.get_json() or {}
        ip_to_remove = data.get("ip", "").strip()
        if ip_to_remove in servers_list:
            servers_list.remove(ip_to_remove)
            save_servers(servers_list)
            SERVER_STATES.pop(ip_to_remove, None)
        return jsonify(servers_list)
    return jsonify(servers_list)

@app.route("/api/stats", methods=["GET"])
def get_stats():
    full_results = []
    for ip in servers_list:
        state = SERVER_STATES.get(ip, {
            "ip": ip, "online": True, "players": 0, "max": 0,
            "version": "Inizializzazione...", "favicon": None
        }).copy()
        
        analytics = get_server_analytics(ip, state["players"])
        state.update(analytics)
        full_results.append(state)
        
    full_results.sort(key=lambda x: (x["online"], x["players"]), reverse=True)
    return jsonify(full_results)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
