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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT, entry REAL, sl REAL, tp1 REAL,
                status TEXT DEFAULT 'PENDING', lot_size REAL DEFAULT 0,
                pnl REAL DEFAULT 0, opened_at TEXT, closed_at TEXT,
                ticket INTEGER DEFAULT 0, reject_reason TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS account_sync (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                equity REAL DEFAULT 0, margin REAL DEFAULT 0, 
                risk_pct REAL DEFAULT 0, daily_pnl REAL DEFAULT 0,
                prop_mode INTEGER DEFAULT 0, last_sync TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                bot_active INTEGER DEFAULT 0,
                news_guard INTEGER DEFAULT 1,
                account_mode TEXT DEFAULT 'DEMO'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS node_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                message TEXT
            )
        """)
        conn.execute("INSERT OR IGNORE INTO account_sync (id, equity, last_sync) VALUES (1, 0, ?)", (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),))
        conn.execute("INSERT OR IGNORE INTO settings (id, bot_active, news_guard, account_mode) VALUES (1, 0, 1, 'DEMO')")
        conn.commit()

init_db()

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify({"error": "Unauthorized"}), 401
            
        action, entry, sl, tp1 = data["action"], float(data["entry"]), float(data["sl"]), float(data["tp1"])
        
        with lock:
            with get_db() as conn:
                status = "PENDING"
                reject_reason = ""
                
                # Blokada weekendowa
                now_utc = datetime.now(timezone.utc)
                if now_utc.weekday() in [5, 6]: 
                    status = "REJECTED"
                    reject_reason = "WEEKEND CLOSED"
                
                conn.execute("UPDATE trades SET status='CANCELED' WHERE status='PENDING'")
                conn.execute("INSERT INTO trades (action, entry, sl, tp1, status, reject_reason) VALUES (?,?,?,?,?,?)", 
                             (action, entry, sl, tp1, status, reject_reason))
                conn.commit()
        return jsonify({"status": "signal_processed", "result": status})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/log", methods=["POST"])
def add_log():
    try:
        data = request.get_json(force=True)
        if data.get("secret") != WEBHOOK_SECRET: return jsonify({"error": "Unauthorized"}), 401
        with lock:
            with get_db() as conn:
                conn.execute("INSERT INTO node_logs (timestamp, message) VALUES (?, ?)", (datetime.now(timezone.utc).strftime("%H:%M:%S"), data["message"]))
                conn.execute("DELETE FROM node_logs WHERE id NOT IN (SELECT id FROM node_logs ORDER BY id DESC LIMIT 50)")
                conn.commit()
        return jsonify({"status": "logged"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/pending", methods=["GET"])
def get_pending():
    secret = request.args.get("secret")
    if secret != WEBHOOK_SECRET: return jsonify({"error": "Unauthorized"}), 401
    with get_db() as conn:
        settings = conn.execute("SELECT * FROM settings WHERE id=1").fetchone()
        trade = conn.execute("SELECT * FROM trades WHERE status='PENDING' ORDER BY id DESC LIMIT 1").fetchone()
        return jsonify({
            "trade": dict(trade) if trade else {},
            "bot_active": settings["bot_active"],
            "news_guard": settings["news_guard"],
            "account_mode": settings["account_mode"]
        })

@app.route("/api/sync", methods=["POST"])
def sync_data():
    payload = request.json
    if payload.get("secret") != WEBHOOK_SECRET: return jsonify({"error": "Unauthorized"}), 401
    now_s = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with lock:
        with get_db() as conn:
            if "equity" in payload:
                conn.execute("UPDATE account_sync SET equity=?, margin=?, risk_pct=?, daily_pnl=?, prop_mode=?, last_sync=? WHERE id=1",
                             (payload["equity"], payload.get("margin", 0), payload["risk_pct"], payload.get("daily_pnl", 0), payload.get("prop_mode", 0), now_s))
            if "trade_update" in payload:
                tu = payload["trade_update"]
                conn.execute("""
                    UPDATE trades SET status=?, ticket=?, lot_size=?, pnl=?, 
                    entry=?, opened_at=COALESCE(opened_at, ?), closed_at=CASE WHEN ? IN ('WIN','LOSS','BE') THEN ? ELSE closed_at END
                    WHERE id=?
                """, (tu["status"], tu.get("ticket",0), tu.get("lot",0), tu.get("pnl",0), tu.get("entry",0), now_s, tu["status"], now_s, tu["id"]))
            conn.commit()
    return jsonify({"status": "synced"})

@app.route("/api/ui_data", methods=["GET"])
def ui_data():
    with get_db() as conn:
        acc = dict(conn.execute("SELECT * FROM account_sync WHERE id=1").fetchone())
        settings = dict(conn.execute("SELECT * FROM settings WHERE id=1").fetchone())
        trades = [dict(t) for t in conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 50").fetchall()]
        logs = [dict(l) for l in conn.execute("SELECT * FROM node_logs ORDER BY id DESC LIMIT 15").fetchall()]
        
        now_utc = datetime.now(timezone.utc)
        last_sync_time = datetime.strptime(acc["last_sync"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        seconds_ago = (now_utc - last_sync_time).total_seconds()
        
        wins = sum(1 for t in trades if t["status"] == "WIN")
        losses = sum(1 for t in trades if t["status"] == "LOSS")
        bes = sum(1 for t in trades if t["status"] == "BE")
        total_pnl = sum(t["pnl"] for t in trades if t["status"] in ["WIN", "LOSS", "BE"])
        
        gross_profit = sum(t["pnl"] for t in trades if t["status"] == "WIN")
        gross_loss = sum(abs(t["pnl"]) for t in trades if t["status"] == "LOSS")
        pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (round(gross_profit, 2) if gross_profit > 0 else 0)
        
        return jsonify({
            "acc": acc, "settings": settings, "trades": trades, "logs": logs[::-1],
            "node_status": "ONLINE" if seconds_ago < 10 else "OFFLINE", "seconds_ago": seconds_ago,
            "stats": {"wins": wins, "losses": losses, "bes": bes, "total_pnl": total_pnl, "profit_factor": pf}
        })

@app.route("/api/toggle/<target>", methods=["POST"])
def toggle(target):
    valid = {"bot": "bot_active", "news": "news_guard", "mode": "account_mode"}
    if target not in valid: return jsonify({"error": "invalid target"}), 400
    col = valid[target]
    with lock:
        with get_db() as conn:
            curr = conn.execute(f"SELECT {col} FROM settings WHERE id=1").fetchone()[col]
            if target == "mode":
                new_val = "LIVE" if curr == "DEMO" else "DEMO"
            else:
                new_val = 1 if curr == 0 else 0
            conn.execute(f"UPDATE settings SET {col}=? WHERE id=1", (new_val,))
            conn.commit()
    return jsonify({"status": "ok", "new_val": new_val})

DASHBOARD = """
<!DOCTYPE html>
<html lang="en" class="antialiased">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Apex Quant | Control Room</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap');
        body { font-family: 'Inter', sans-serif; background-color: #09090b; color: #fafafa; background-image: radial-gradient(circle at 50% 0%, rgba(39, 39, 42, 0.4) 0%, transparent 70%); background-attachment: fixed; }
        .mono { font-family: 'JetBrains Mono', monospace; }
        .premium-card { background: rgba(24, 24, 27, 0.6); backdrop-filter: blur(16px); border: 1px solid rgba(255, 255, 255, 0.08); box-shadow: 0 4px 30px rgba(0, 0, 0, 0.5); border-radius: 1rem; transition: all 0.3s ease; }
        .text-accent-green { color: #10b981; text-shadow: 0 0 20px rgba(16, 185, 129, 0.2); }
        .text-accent-red { color: #f43f5e; text-shadow: 0 0 20px rgba(244, 63, 94, 0.2); }
        .text-accent-gold { color: #fbbf24; text-shadow: 0 0 20px rgba(251, 191, 36, 0.2); }
        
        .toggle-checkbox:checked { right: 0; border-color: #10b981; }
        .toggle-checkbox:checked + .toggle-label { background-color: #10b981; }
        .toggle-checkbox.mode:checked { border-color: #f43f5e; }
        .toggle-checkbox.mode:checked + .toggle-label { background-color: #f43f5e; }
        .toggle-checkbox.news:checked { border-color: #3b82f6; }
        .toggle-checkbox.news:checked + .toggle-label { background-color: #3b82f6; }
        .toggle-checkbox:checked + .toggle-label:before { transform: translateX(100%); }

        .pulse-dot { height: 8px; width: 8px; border-radius: 50%; display: inline-block; }
        .pulse-online { background-color: #10b981; box-shadow: 0 0 10px #10b981; animation: pulse-g 2s infinite; }
        .pulse-offline { background-color: #f43f5e; box-shadow: 0 0 10px #f43f5e; }
        @keyframes pulse-g { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.5; transform: scale(1.2); } }
        
        .custom-scrollbar::-webkit-scrollbar { height: 6px; width: 6px; }
        .custom-scrollbar::-webkit-scrollbar-track { background: rgba(0,0,0,0.2); border-radius: 10px; }
        .custom-scrollbar::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 10px; }
    </style>
</head>
<body class="min-h-screen p-3 md:p-6 lg:p-8 flex flex-col gap-4">

    <header class="flex flex-col xl:flex-row justify-between items-start xl:items-center gap-4 premium-card p-4">
        <div class="flex items-center gap-4">
            <div class="w-12 h-12 rounded-xl bg-zinc-800 flex items-center justify-center border border-white/10 shadow-lg">
                <i class="fa-solid fa-layer-group text-xl text-zinc-300"></i>
            </div>
            <div>
                <h1 class="text-xl md:text-2xl font-bold tracking-tight">Apex <span class="font-light text-zinc-400">Quant</span></h1>
                <div class="text-[10px] md:text-xs uppercase tracking-widest flex items-center gap-2 mt-1 font-medium">
                    <span id="node-dot" class="pulse-dot pulse-offline"></span><span id="node-status" class="text-zinc-500">System Offline</span>
                </div>
            </div>
        </div>
        
        <div class="hidden xl:flex gap-6 border-l border-white/10 pl-6">
            <div class="flex flex-col"><span class="text-[9px] text-zinc-500 font-bold uppercase tracking-wider">London</span><span class="mono text-sm" id="clock-ldn">--:--</span></div>
            <div class="flex flex-col"><span class="text-[9px] text-zinc-500 font-bold uppercase tracking-wider">New York</span><span class="mono text-sm" id="clock-ny">--:--</span></div>
            <div class="flex flex-col"><span class="text-[9px] text-zinc-500 font-bold uppercase tracking-wider">Tokyo</span><span class="mono text-sm" id="clock-tok">--:--</span></div>
        </div>

        <div class="flex items-center gap-6 bg-black/30 p-2.5 rounded-xl border border-white/5 w-full xl:w-auto justify-between xl:justify-end">
            <div class="flex flex-col items-center">
                <span class="text-[8px] text-zinc-500 font-bold uppercase mb-1" id="mode-txt">DEMO ACCOUNT</span>
                <div class="relative inline-block w-8 h-4"><input type="checkbox" id="mode-toggle" class="toggle-checkbox mode absolute block w-4 h-4 rounded-full bg-white border-2 border-zinc-700 appearance-none cursor-pointer z-10" onclick="toggleSetting('mode')"/><label for="mode-toggle" class="toggle-label block overflow-hidden h-4 rounded-full bg-zinc-700 cursor-pointer transition-colors duration-300"></label></div>
            </div>
            <div class="w-px h-6 bg-white/10"></div>
            <div class="flex flex-col items-center">
                <span class="text-[8px] text-blue-400 font-bold uppercase mb-1">NEWS GUARD</span>
                <div class="relative inline-block w-8 h-4"><input type="checkbox" id="news-toggle" class="toggle-checkbox news absolute block w-4 h-4 rounded-full bg-white border-2 border-zinc-700 appearance-none cursor-pointer z-10" onclick="toggleSetting('news')"/><label for="news-toggle" class="toggle-label block overflow-hidden h-4 rounded-full bg-zinc-700 cursor-pointer transition-colors duration-300"></label></div>
            </div>
            <div class="w-px h-6 bg-white/10"></div>
            <div class="flex flex-col items-center">
                <span class="text-[8px] text-emerald-400 font-bold uppercase mb-1">BOT ENGINE</span>
                <div class="relative inline-block w-8 h-4"><input type="checkbox" id="bot-toggle" class="toggle-checkbox absolute block w-4 h-4 rounded-full bg-white border-2 border-zinc-700 appearance-none cursor-pointer z-10" onclick="toggleSetting('bot')"/><label for="bot-toggle" class="toggle-label block overflow-hidden h-4 rounded-full bg-zinc-700 cursor-pointer transition-colors duration-300"></label></div>
            </div>
        </div>
    </header>

    <div class="premium-card p-4 border border-blue-500/30 bg-blue-900/10 flex justify-between items-center relative overflow-hidden">
        <div class="absolute inset-0 bg-gradient-to-r from-blue-500/10 to-transparent pointer-events-none"></div>
        <div class="flex items-center gap-4 relative z-10">
            <i class="fa-solid fa-earth-americas text-blue-400 text-3xl animate-pulse drop-shadow-[0_0_15px_rgba(59,130,246,0.8)]"></i>
            <div>
                <h3 class="text-[10px] text-blue-400 font-bold uppercase tracking-widest drop-shadow-md">Macroeconomic Radar Active (Clear)</h3>
                <p class="text-zinc-100 font-semibold mt-1">Awaiting Data Events</p>
            </div>
        </div>
    </div>
    
    <div class="premium-card p-4 relative overflow-hidden border-t border-t-indigo-500/50">
        <div class="flex justify-between items-center mb-2">
            <h3 class="text-[10px] text-zinc-400 font-bold uppercase flex items-center gap-2"><i class="fa-solid fa-shield-halved text-indigo-400"></i> FTMO Guardian (Daily DD)</h3>
            <span class="text-xs font-mono font-bold" id="ui-daily-txt">$0.00</span>
        </div>
        <div class="w-full bg-zinc-800 rounded-full h-2 mt-3">
            <div class="bg-indigo-500 h-2 rounded-full transition-all duration-500 shadow-[0_0_10px_rgba(99,102,241,0.6)]" id="ui-dd-bar" style="width: 0%"></div>
        </div>
        <div class="flex justify-between mt-2 text-[9px] text-zinc-500 uppercase font-bold"><span>0% (Safe)</span><span class="text-rose-500">4% (Killswitch)</span></div>
    </div>

    <div class="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <div class="premium-card p-4"><h3 class="text-[9px] text-zinc-400 font-bold uppercase mb-1">Live Equity</h3><div class="mono text-2xl font-bold text-accent-green" id="ui-eq">$0.00</div><div class="text-[9px] text-zinc-500 mt-1 uppercase">MT5 Terminal Sync</div></div>
        <div class="premium-card p-4"><h3 class="text-[9px] text-zinc-400 font-bold uppercase mb-1">Net P&L</h3><div class="mono text-2xl font-bold" id="ui-pnl">$0.00</div><div class="text-[9px] text-zinc-500 mt-1 uppercase">Session Total</div></div>
        <div class="premium-card p-4"><h3 class="text-[9px] text-zinc-400 font-bold uppercase mb-1">Profit Factor</h3><div class="mono text-2xl font-bold text-zinc-100" id="ui-pf">0.00</div><div class="text-[9px] text-zinc-500 mt-1 uppercase">Gross Win / Gross Loss</div></div>
        <div class="premium-card p-4"><h3 class="text-[9px] text-zinc-400 font-bold uppercase mb-1">Risk Target</h3><div class="mono text-2xl font-bold text-blue-400" id="ui-risk">0.0%</div><div class="text-[8px] font-bold mt-1 uppercase text-indigo-400" id="ui-mode-txt">Evaluating...</div></div>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-3 gap-4 flex-grow">
        <div class="flex flex-col gap-4 lg:col-span-1">
            <div class="premium-card p-4 flex flex-col h-[220px]">
                <div class="flex justify-between items-center mb-2">
                    <h3 class="text-[10px] text-zinc-400 font-bold uppercase">Equity Curve</h3>
                    <span class="text-[9px] font-bold text-zinc-500" id="ui-winrate">WR: 0%</span>
                </div>
                <div class="flex-grow relative w-full"><canvas id="pnlChart"></canvas></div>
                <div class="flex justify-between mt-2 px-1">
                    <span class="text-[9px] font-bold text-emerald-500" id="ui-wins">0 W</span>
                    <span class="text-[9px] font-bold text-zinc-500" id="ui-bes">0 BE</span>
                    <span class="text-[9px] font-bold text-rose-500" id="ui-losses">0 L</span>
                </div>
            </div>
            
            <div class="premium-card p-4 flex flex-col flex-grow min-h-[180px]">
                <h3 class="text-[10px] text-zinc-400 font-bold uppercase mb-2">Live Node Console</h3>
                <div id="console-log" class="flex-grow bg-black/60 rounded-lg p-3 font-mono text-[9px] text-emerald-400 overflow-y-auto custom-scrollbar"></div>
            </div>
        </div>

        <div class="premium-card p-4 lg:col-span-2 flex flex-col overflow-hidden h-[415px]">
            <div class="flex justify-between items-center mb-3">
                <h3 class="text-[10px] text-zinc-400 font-bold uppercase">Algorithmic Ledger</h3>
                <span class="text-[8px] text-zinc-400 bg-zinc-800/80 px-2 py-1 rounded-md border border-white/5 uppercase flex items-center gap-1.5"><span class="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse"></span> Live Sync</span>
            </div>
            <div class="overflow-y-auto custom-scrollbar flex-grow">
                <table class="w-full text-left border-collapse whitespace-nowrap">
                    <thead class="sticky top-0 bg-[#18181b] z-10 shadow-sm">
                        <tr>
                            <th class="p-2 text-[9px] uppercase text-zinc-500 border-b border-white/5">Ticket</th>
                            <th class="p-2 text-[9px] uppercase text-zinc-500 border-b border-white/5">Side</th>
                            <th class="p-2 text-[9px] uppercase text-zinc-500 border-b border-white/5">Entry / Size</th>
                            <th class="p-2 text-[9px] uppercase text-zinc-500 border-b border-white/5">Status</th>
                            <th class="p-2 text-[9px] uppercase text-zinc-500 border-b border-white/5 text-right">Net P&L</th>
                        </tr>
                    </thead>
                    <tbody id="trades-body" class="text-[10px] mono"></tbody>
                </table>
            </div>
        </div>
    </div>

    <script>
        function updateClocks() {
            const fmt = (tz) => new Date().toLocaleTimeString('en-US', { timeZone: tz, hour12: false, hour: '2-digit', minute:'2-digit' });
            document.getElementById('clock-ldn').innerText = fmt('Europe/London');
            document.getElementById('clock-ny').innerText = fmt('America/New_York');
            document.getElementById('clock-tok').innerText = fmt('Asia/Tokyo');
        } setInterval(updateClocks, 1000); updateClocks();

        const ctx = document.getElementById('pnlChart').getContext('2d');
        let grad = ctx.createLinearGradient(0, 0, 0, 200); grad.addColorStop(0, 'rgba(16, 185, 129, 0.4)'); grad.addColorStop(1, 'rgba(16, 185, 129, 0.0)');
        Chart.defaults.color = '#71717a'; Chart.defaults.font.family = "'JetBrains Mono', monospace";
        const pnlChart = new Chart(ctx, { type: 'line', data: { labels: [], datasets: [{ data: [], borderColor: '#10b981', backgroundColor: grad, borderWidth: 2, fill: true, tension: 0.3, pointRadius: 1 }] }, options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { display: false }, y: { grid: { color: 'rgba(255,255,255,0.03)' } } } } });

        async function toggleSetting(target) { await fetch('/api/toggle/' + target, { method: 'POST' }); fetchData(); }
        function fmtM(amt) { return amt >= 0 ? "+$" + amt.toFixed(2) : "-$" + Math.abs(amt).toFixed(2); }

        async function fetchData() {
            try {
                const res = await fetch('/api/ui_data'); const data = await res.json();
                
                document.getElementById('bot-toggle').checked = data.settings.bot_active === 1;
                document.getElementById('news-toggle').checked = data.settings.news_guard === 1;
                document.getElementById('mode-toggle').checked = data.settings.account_mode === 'LIVE';
                let mt = document.getElementById('mode-txt');
                mt.innerText = data.settings.account_mode + " ACCOUNT"; mt.className = data.settings.account_mode === 'LIVE' ? "text-[8px] font-bold uppercase mb-1 text-rose-400" : "text-[8px] font-bold uppercase mb-1 text-zinc-500";

                document.getElementById('ui-eq').innerText = "$" + data.acc.equity.toFixed(2);
                document.getElementById('ui-risk').innerText = data.acc.risk_pct.toFixed(2) + "%";
                document.getElementById('ui-pf').innerText = data.stats.profit_factor.toFixed(2);
                
                let pnlEl = document.getElementById('ui-pnl'); pnlEl.innerText = fmtM(data.stats.total_pnl); pnlEl.className = data.stats.total_pnl >= 0 ? "mono text-2xl font-bold text-accent-green" : "mono text-2xl font-bold text-accent-red";

                document.getElementById('ui-wins').innerText = data.stats.wins + " W"; document.getElementById('ui-losses').innerText = data.stats.losses + " L"; document.getElementById('ui-bes').innerText = data.stats.bes + " BE";
                document.getElementById('ui-winrate').innerText = "WR: " + ((data.stats.wins+data.stats.losses)>0 ? Math.round((data.stats.wins/(data.stats.wins+data.stats.losses))*100) : 0) + "%";

                const isProp = data.acc.prop_mode === 1;
                let mdTxt = document.getElementById('ui-mode-txt'); mdTxt.innerText = isProp ? "FTMO / PROP FIRM MODE" : "PRIVATE (KELLY COMPOUNDING)"; mdTxt.className = isProp ? "text-[8px] font-bold mt-1 uppercase text-indigo-400" : "text-[8px] font-bold mt-1 uppercase text-amber-500";
                
                let ddPnl = data.acc.daily_pnl; document.getElementById('ui-daily-txt').innerText = fmtM(ddPnl); document.getElementById('ui-daily-txt').className = ddPnl >= 0 ? "text-xs font-mono font-bold text-emerald-400" : "text-xs font-mono font-bold text-rose-400";
                let ddPct = (ddPnl < 0 && data.acc.equity > 0) ? (Math.abs(ddPnl) / (data.acc.equity - ddPnl)) * 100 : 0;
                let bW = Math.min((ddPct / 4.0) * 100, 100); document.getElementById('ui-dd-bar').style.width = bW + "%"; document.getElementById('ui-dd-bar').className = bW > 80 ? "bg-rose-500 h-2 rounded-full transition-all duration-500 shadow-[0_0_10px_rgba(244,63,94,0.6)]" : "bg-indigo-500 h-2 rounded-full transition-all duration-500 shadow-[0_0_10px_rgba(99,102,241,0.6)]";

                let s = document.getElementById('node-status'); let d = document.getElementById('node-dot');
                if(data.node_status==="ONLINE") { d.className="pulse-dot pulse-online"; s.innerText="ONLINE ("+Math.round(data.seconds_ago)+"s)"; s.className="text-emerald-400 font-bold"; }
                else { d.className="pulse-dot pulse-offline"; s.innerText="OFFLINE"; s.className="text-rose-400 font-bold"; }

                let cl = document.getElementById('console-log'); let scrl = cl.scrollTop+cl.clientHeight>=cl.scrollHeight-20; cl.innerHTML='';
                data.logs.forEach(l => { cl.innerHTML += `<p><span class="text-zinc-600">[${l.timestamp}]</span> <span class="text-emerald-400">${l.message}</span></p>`; });
                if(scrl) cl.scrollTop = cl.scrollHeight;

                let tb = document.getElementById('trades-body'); tb.innerHTML='';
                let cLbl = ['S']; let cDat = [0]; let cPnl = 0;
                data.trades.forEach(t => {
                    let bc = t.status==='WIN'?'bg-emerald-500/10 text-emerald-400':t.status==='LOSS'?'bg-rose-500/10 text-rose-400':t.status==='BE'?'bg-zinc-500/10 text-zinc-400':t.status.includes('REJ')?'bg-purple-500/10 text-purple-400':'bg-amber-500/10 text-amber-400';
                    let tc = t.action==='LONG'?'text-emerald-500':'text-rose-500';
                    let pc = t.pnl>0?'text-emerald-400':t.pnl<0?'text-rose-400':'text-zinc-500';
                    tb.innerHTML += `<tr class="border-b border-white/5 hover:bg-white/5"><td class="p-2 text-zinc-500">#${t.ticket||'WAIT'}</td><td class="p-2 font-bold ${tc}">${t.action}</td><td class="p-2 text-zinc-300">@${t.entry.toFixed(2)}</td><td class="p-2"><span class="px-1.5 py-0.5 rounded text-[8px] font-bold ${bc}">${t.status}</span></td><td class="p-2 font-bold text-right ${pc}">${t.status.includes('REJ')||t.status==='PENDING'?'---':fmtM(t.pnl)}</td></tr>`;
                });
                data.trades.filter(t=>['WIN','LOSS','BE'].includes(t.status)).reverse().forEach((t,i) => { cPnl+=t.pnl; cLbl.push('#'+(i+1)); cDat.push(cPnl); });
                pnlChart.data.datasets[0].borderColor = cPnl<0?'#f43f5e':'#10b981'; pnlChart.data.labels = cLbl; pnlChart.data.datasets[0].data = cDat; pnlChart.update();
            } catch (e) {}
        } fetchData(); setInterval(fetchData, 1500);
    </script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(DASHBOARD)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
