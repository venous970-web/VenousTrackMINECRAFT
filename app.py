import concurrent.futures
import json
import os
import sqlite3
import time
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
from mcstatus import JavaServer

app = Flask(__name__)
CORS(app)

DB_FILE = "venous_track.db"
CONFIG_FILE = "servers.json"

SERVER_STATES = {}
MAX_FAILS = 3
PING_INTERVAL = 15  # Intervallo del ping reale verso i server Minecraft (15 secondi)
ITALY_TZ = ZoneInfo("Europe/Rome")

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
    try:
        server = JavaServer.lookup(ip)
        return server.status()
    except Exception:
        pass
        
    try:
        if ":" in ip:
            host, port_str = ip.split(":")
            server = JavaServer(host, int(port_str))
        else:
            server = JavaServer(ip, 25565)
        return server.status()
    except Exception:
        return None

def background_tracker():
    while True:
        now_ts = int(time.time())
        current_servers = list(servers_list)
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
            ping_results = list(executor.map(lambda ip: (ip, query_minecraft_server(ip)), current_servers))
            
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        
        for ip, status in ping_results:
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
                try:
                    players = status.players.online
                    max_p = status.players.max
                    version = status.version.name if hasattr(status.version, 'name') else "1.7.2-1.21"
                    favicon = status.icon if hasattr(status, 'icon') and status.icon else state["favicon"]
                    
                    state["fail_count"] = 0
                    state["online"] = True
                    state["players"] = players
                    state["max"] = max_p
                    state["version"] = version
                    state["favicon"] = favicon
                    
                    c.execute("INSERT INTO server_stats VALUES (?, ?, ?, ?)", (ip, now_ts, players, max_p))
                except Exception:
                    state["fail_count"] += 1
            else:
                state["fail_count"] += 1

            if state["fail_count"] >= MAX_FAILS:
                state["online"] = False
                state["players"] = 0
                state["version"] = "Non raggiungibile"

        conn.commit()
        conn.close()
        
        time.sleep(PING_INTERVAL)

tracking_thread = threading.Thread(target=background_tracker, daemon=True)
tracking_thread.start()

def get_server_analytics(ip, current_players):
    now_ts = int(time.time())
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    ts_72h = now_ts - (72 * 3600)
    c.execute("SELECT MAX(players) FROM server_stats WHERE ip = ? AND timestamp >= ?", (ip, ts_72h))
    row_72h = c.fetchone()
    peak_72h = row_72h[0] if row_72h and row_72h[0] is not None else current_players
    
    c.execute("SELECT players, timestamp FROM server_stats WHERE ip = ? ORDER BY players DESC LIMIT 1", (ip,))
    row_rec = c.fetchone()
    if row_rec and row_rec[0] is not None:
        rec_players = row_rec[0]
        rec_date = datetime.fromtimestamp(row_rec[1], ITALY_TZ).strftime("%d/%m/%Y")
    else:
        rec_players = current_players
        rec_date = datetime.now(ITALY_TZ).strftime("%d/%m/%Y")
        
    def get_past_players(seconds_ago):
        target_ts = now_ts - seconds_ago
        c.execute("SELECT players FROM server_stats WHERE ip = ? AND timestamp <= ? ORDER BY timestamp DESC LIMIT 1", (ip, target_ts))
        r = c.fetchone()
        if r and r[0] is not None:
            try:
                return f"{int(r[0]):,}".replace(",", ".")
            except ValueError:
                return str(r[0])
        return "-"
        
    p_1d = get_past_players(86400)
    p_2d = get_past_players(2 * 86400)
    p_3d = get_past_players(3 * 86400)
    
    c.execute("SELECT timestamp, players FROM server_stats WHERE ip = ? ORDER BY timestamp DESC LIMIT 25", (ip,))
    rows_chart = c.fetchall()
    conn.close()
    
    rows_chart.reverse()
    chart_labels = [datetime.fromtimestamp(r[0], ITALY_TZ).strftime("%H:%M:%S") for r in rows_chart]
    chart_data = [r[1] for r in rows_chart]
    
    if len(chart_data) < 2:
        now_str = datetime.now(ITALY_TZ).strftime("%H:%M:%S")
        chart_labels = ["--:--:--", now_str]
        chart_data = [current_players, current_players]
        
    peak_72h_str = f"{int(peak_72h):,}".replace(",", ".") if peak_72h is not None else "0"
    record_str = f"{int(rec_players):,} ({rec_date})".replace(",", ".") if rec_players is not None else f"0 ({rec_date})"

    return {
        "peak_72h": peak_72h_str,
        "record": record_str,
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
