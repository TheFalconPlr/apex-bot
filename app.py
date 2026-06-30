import os
import sqlite3
import threading
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "Potrzta2012.8987")
DB_PATH = "quant_data.db"
lock = threading.Lock()

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT, entry REAL, sl REAL, tp1 REAL, status TEXT DEFAULT 'PENDING', lot_size REAL DEFAULT 0, pnl REAL DEFAULT 0, opened_at TEXT, closed_at TEXT, ticket INTEGER DEFAULT 0, reject_reason TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS account_sync (id INTEGER PRIMARY KEY CHECK (id = 1), equity REAL DEFAULT 0, margin REAL DEFAULT 0, risk_pct REAL DEFAULT 0, daily_pnl REAL DEFAULT 0, prop_mode INTEGER DEFAULT 0, last_sync TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK (id = 1), bot_active INTEGER DEFAULT 0, account_mode TEXT DEFAULT 'DEMO')""")
        conn.execute("""CREATE TABLE IF NOT EXISTS node_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, message TEXT)""")
        conn.execute("INSERT OR IGNORE INTO account_sync (id, equity, last_sync) VALUES (1, 0, ?)", (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),))
        conn.execute("INSERT OR IGNORE INTO settings (id, bot_active, account_mode) VALUES (1, 0, 'DEMO')")
        conn.commit()
init_db()

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        if data.get("secret") != WEBHOOK_SECRET: return jsonify({"error": "Unauthorized"}), 401
        with lock:
            with get_db() as conn:
                status, reject_reason = "PENDING", ""
                if datetime.now(timezone.utc).weekday() in [5, 6]: status, reject_reason = "REJECTED", "WEEKEND CLOSED"
                conn.execute("UPDATE trades SET status='CANCELED' WHERE status='PENDING'")
                conn.execute("INSERT INTO trades (action, entry, sl, tp1, status, reject_reason) VALUES (?,?,?,?,?,?)", (data["action"], float(data["entry"]), float(data["sl"]), float(data["tp1"]), status, reject_reason))
                conn.commit()
        return jsonify({"status": "signal_processed"})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/api/log", methods=["POST"])
def add_log():
    data = request.get_json(force=True)
    if data.get("secret") != WEBHOOK_SECRET: return jsonify({"error": "Unauthorized"}), 401
    with lock:
        with get_db() as conn:
            conn.execute("INSERT INTO node_logs (timestamp, message) VALUES (?, ?)", (datetime.now(timezone.utc).strftime("%H:%M:%S"), data["message"]))
            conn.execute("DELETE FROM node_logs WHERE id NOT IN (SELECT id FROM node_logs ORDER BY id DESC LIMIT 50)")
            conn.commit()
    return jsonify({"status": "logged"})

@app.route("/api/pending", methods=["GET"])
def get_pending():
    if request.args.get("secret") != WEBHOOK_SECRET: return jsonify({"error": "Unauthorized"}), 401
    with get_db() as conn:
        settings = conn.execute("SELECT bot_active FROM settings WHERE id=1").fetchone()
        trade = conn.execute("SELECT * FROM trades WHERE status='PENDING' ORDER BY id DESC LIMIT 1").fetchone()
        return jsonify({"trade": dict(trade) if trade else {}, "bot_active": settings["bot_active"] if settings else 0})

@app.route("/api/sync", methods=["POST"])
def sync_data():
    payload = request.json
    if payload.get("secret") != WEBHOOK_SECRET: return jsonify({"error": "Unauthorized"}), 401
    with lock:
        with get_db() as conn:
            if "equity" in payload:
                conn.execute("UPDATE account_sync SET equity=?, risk_pct=?, daily_pnl=?, prop_mode=?, last_sync=? WHERE id=1", (payload["equity"], payload["risk_pct"], payload.get("daily_pnl", 0), payload.get("prop_mode", 0), datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")))
            if "trade_update" in payload:
                tu = payload["trade_update"]
                now_s = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute("UPDATE trades SET status=?, ticket=?, lot_size=?, entry=?, opened_at=COALESCE(opened_at, ?) WHERE id=?", (tu["status"], tu.get("ticket",0), tu.get("lot",0), tu.get("entry",0), now_s, tu["id"]))
            conn.commit()
    return jsonify({"status": "synced"})

@app.route("/api/ui_data", methods=["GET"])
def ui_data():
    with get_db() as conn:
        acc = dict(conn.execute("SELECT * FROM account_sync WHERE id=1").fetchone())
        settings = dict(conn.execute("SELECT * FROM settings WHERE id=1").fetchone())
        trades = [dict(t) for t in conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 50").fetchall()]
        logs = [dict(l) for l in conn.execute("SELECT * FROM node_logs ORDER BY id DESC LIMIT 15").fetchall()]
        
        last_sync = datetime.strptime(acc["last_sync"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        sec_ago = (datetime.now(timezone.utc) - last_sync).total_seconds()
        
        return jsonify({"acc": acc, "settings": settings, "trades": trades, "logs": logs[::-1], "status": "ONLINE" if sec_ago < 10 else "OFFLINE", "ping": sec_ago})

@app.route("/api/toggle/bot", methods=["POST"])
def toggle():
    with lock:
        with get_db() as conn:
            curr = conn.execute("SELECT bot_active FROM settings WHERE id=1").fetchone()["bot_active"]
            conn.execute("UPDATE settings SET bot_active=? WHERE id=1", (0 if curr == 1 else 1,))
            conn.commit()
    return jsonify({"status": "ok"})

DASHBOARD = """
<!DOCTYPE html><html lang="en" class="antialiased"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Apex Quant | Enterprise</title><script src="https://cdn.tailwindcss.com"></script><link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet"><style>body{background-color:#09090b;color:#fafafa;font-family:system-ui,-apple-system,sans-serif;}.glass{background:rgba(24,24,27,0.7);backdrop-filter:blur(12px);border:1px solid rgba(255,255,255,0.05);border-radius:1rem;}.pulse{animation:pulse 2s cubic-bezier(0.4,0,0.6,1) infinite;}@keyframes pulse{0%,100%{opacity:1;}50%{opacity:.5;}}</style></head>
<body class="p-4 md:p-8 flex flex-col gap-6">
    <header class="glass p-5 flex flex-col md:flex-row justify-between items-center gap-4">
        <div class="flex items-center gap-4">
            <div class="w-12 h-12 bg-zinc-800 rounded-xl flex justify-center items-center text-xl shadow-lg border border-white/10"><i class="fa-solid fa-microchip text-zinc-300"></i></div>
            <div>
                <h1 class="text-xl font-bold tracking-tight">Apex <span class="font-light text-zinc-400">Enterprise</span></h1>
                <div class="text-xs font-bold uppercase tracking-wider flex items-center gap-2 mt-1" id="ui-status"><span class="w-2 h-2 rounded-full bg-rose-500"></span> OFFLINE</div>
            </div>
        </div>
        <div class="flex items-center gap-6 bg-black/40 px-6 py-3 rounded-xl border border-white/5">
            <div class="flex flex-col"><span class="text-[10px] text-zinc-500 font-bold uppercase">Engine Status</span>
                <label class="relative inline-flex items-center cursor-pointer mt-1">
                    <input type="checkbox" id="bot-toggle" class="sr-only peer" onchange="toggleBot()">
                    <div class="w-9 h-5 bg-zinc-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-emerald-500"></div>
                </label>
            </div>
        </div>
    </header>

    <div class="grid grid-cols-1 md:grid-cols-4 gap-4">
        <div class="glass p-5"><h3 class="text-[10px] text-zinc-400 font-bold uppercase mb-2">Live Equity</h3><div class="text-3xl font-mono font-bold text-emerald-400" id="ui-eq">$0.00</div></div>
        <div class="glass p-5"><h3 class="text-[10px] text-zinc-400 font-bold uppercase mb-2">Current Risk Mode</h3><div class="text-3xl font-mono font-bold text-blue-400" id="ui-risk">0.0%</div><div class="text-[10px] text-zinc-500 mt-1 uppercase" id="ui-mode-txt">Evaluating...</div></div>
        
        <!-- FTMO GUARDIAN UI -->
        <div class="glass p-5 md:col-span-2 relative overflow-hidden border-t-2 border-t-indigo-500/50">
            <div class="flex justify-between items-center mb-2">
                <h3 class="text-[10px] text-zinc-400 font-bold uppercase flex items-center gap-2"><i class="fa-solid fa-shield-halved text-indigo-400"></i> FTMO Guardian (Daily DD)</h3>
                <span class="text-xs font-mono font-bold" id="ui-daily-txt">$0.00</span>
            </div>
            <div class="w-full bg-zinc-800 rounded-full h-2.5 mt-4">
                <div class="bg-indigo-500 h-2.5 rounded-full transition-all duration-500" id="ui-dd-bar" style="width: 0%"></div>
            </div>
            <div class="flex justify-between mt-2 text-[9px] text-zinc-500 uppercase font-bold"><span>0% (Safe)</span><span class="text-rose-500">4% (Killswitch)</span></div>
        </div>
    </div>

    <div class="grid grid-cols-1 md:grid-cols-3 gap-6">
        <div class="glass p-5 col-span-1 flex flex-col h-[300px]">
            <h3 class="text-[10px] text-zinc-400 font-bold uppercase mb-3">Live Console</h3>
            <div id="console" class="flex-grow bg-black/50 rounded-lg p-3 font-mono text-[10px] text-emerald-400 overflow-y-auto"></div>
        </div>
        <div class="glass p-5 col-span-2 overflow-auto h-[300px]">
            <h3 class="text-[10px] text-zinc-400 font-bold uppercase mb-3">Order Flow</h3>
            <table class="w-full text-left whitespace-nowrap"><tbody id="trades" class="text-xs font-mono text-zinc-300"></tbody></table>
        </div>
    </div>

    <script>
        async function toggleBot() { await fetch('/api/toggle/bot', {method:'POST'}); }
        async function fetchUI() {
            try {
                const res = await fetch('/api/ui_data'); const data = await res.json();
                document.getElementById('bot-toggle').checked = data.settings.bot_active === 1;
                document.getElementById('ui-eq').innerText = "$" + data.acc.equity.toFixed(2);
                document.getElementById('ui-risk').innerText = data.acc.risk_pct.toFixed(2) + "%";
                
                // Prop Firm UI Logic
                const isProp = data.acc.prop_mode === 1;
                document.getElementById('ui-mode-txt').innerText = isProp ? "FTMO / PROP FIRM MODE" : "PRIVATE (KELLY COMPOUNDING)";
                document.getElementById('ui-mode-txt').className = isProp ? "text-[10px] font-bold text-indigo-400 mt-1 uppercase" : "text-[10px] font-bold text-amber-500 mt-1 uppercase";
                
                let dailyPnl = data.acc.daily_pnl;
                document.getElementById('ui-daily-txt').innerText = (dailyPnl >= 0 ? "+$" : "-$") + Math.abs(dailyPnl).toFixed(2);
                document.getElementById('ui-daily-txt').className = dailyPnl >= 0 ? "text-xs font-mono font-bold text-emerald-400" : "text-xs font-mono font-bold text-rose-400";
                
                let ddPct = 0;
                if (dailyPnl < 0 && data.acc.equity > 0) {
                    let startBal = data.acc.equity - dailyPnl;
                    ddPct = (Math.abs(dailyPnl) / startBal) * 100;
                }
                let barWidth = Math.min((ddPct / 4.0) * 100, 100);
                document.getElementById('ui-dd-bar').style.width = barWidth + "%";
                document.getElementById('ui-dd-bar').className = barWidth > 80 ? "bg-rose-500 h-2.5 rounded-full" : "bg-indigo-500 h-2.5 rounded-full";

                const s = document.getElementById('ui-status');
                if(data.status==="ONLINE") { s.innerHTML = `<span class="w-2 h-2 rounded-full bg-emerald-500 pulse"></span> ONLINE (${Math.round(data.ping)}s)`; s.className="text-xs font-bold uppercase tracking-wider flex items-center gap-2 mt-1 text-emerald-400"; }
                else { s.innerHTML = `<span class="w-2 h-2 rounded-full bg-rose-500"></span> OFFLINE (Check MT5)`; s.className="text-xs font-bold uppercase tracking-wider flex items-center gap-2 mt-1 text-rose-400"; }
                
                const c = document.getElementById('console'); c.innerHTML = '';
                data.logs.forEach(l => { c.innerHTML += `<div class="mb-1"><span class="text-zinc-600">[${l.timestamp}]</span> ${l.message}</div>`; });
                
                const t = document.getElementById('trades'); t.innerHTML = '';
                data.trades.forEach(tr => { t.innerHTML += `<tr class="border-b border-white/5"><td class="py-2">#${tr.ticket||'WAIT'}</td><td class="font-bold ${tr.action==='LONG'?'text-emerald-500':'text-rose-500'}">${tr.action}</td><td>@${tr.entry.toFixed(2)}</td><td><span class="bg-white/5 px-2 py-0.5 rounded">${tr.status}</span></td></tr>`; });
            } catch(e) {}
        }
        fetchUI(); setInterval(fetchUI, 1500);
    </script>
</body></html>
"""
@app.route("/")
def index(): return render_template_string(DASHBOARD)
if __name__ == "__main__": app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
