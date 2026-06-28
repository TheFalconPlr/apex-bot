import os
import sqlite3
import threading
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "KluczDany2026!")
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
                ticket INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS account_sync (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                equity REAL DEFAULT 0, margin REAL DEFAULT 0, 
                risk_pct REAL DEFAULT 0, last_sync TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                bot_active INTEGER DEFAULT 0,
                account_mode TEXT DEFAULT 'DEMO'
            )
        """)
        conn.execute("INSERT OR IGNORE INTO account_sync (id, equity, last_sync) VALUES (1, 0, ?)", (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),))
        conn.execute("INSERT OR IGNORE INTO settings (id, bot_active, account_mode) VALUES (1, 0, 'DEMO')")
        conn.commit()

init_db()

# --- WEBHOOK (Sygnały z TV) ---
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify({"error": "Unauthorized"}), 401
            
        action, entry, sl, tp1 = data["action"], float(data["entry"]), float(data["sl"]), float(data["tp1"])
        
        with lock:
            with get_db() as conn:
                conn.execute("UPDATE trades SET status='CANCELED' WHERE status='PENDING'")
                conn.execute("INSERT INTO trades (action, entry, sl, tp1, status) VALUES (?,?,?,?,'PENDING')", (action, entry, sl, tp1))
                conn.commit()
        return jsonify({"status": "signal_received"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- MT5 ENGINE API ---
@app.route("/api/pending", methods=["GET"])
def get_pending():
    secret = request.args.get("secret")
    if secret != WEBHOOK_SECRET: return jsonify({"error": "Unauthorized"}), 401
    
    with get_db() as conn:
        settings = conn.execute("SELECT bot_active FROM settings WHERE id=1").fetchone()
        if not settings or settings["bot_active"] == 0:
            return jsonify({}) 
            
        trade = conn.execute("SELECT * FROM trades WHERE status='PENDING' ORDER BY id DESC LIMIT 1").fetchone()
        return jsonify(dict(trade) if trade else {})

@app.route("/api/sync", methods=["POST"])
def sync_data():
    secret = request.json.get("secret")
    if secret != WEBHOOK_SECRET: return jsonify({"error": "Unauthorized"}), 401
    
    payload = request.json
    now_s = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    
    with lock:
        with get_db() as conn:
            if "equity" in payload:
                conn.execute("UPDATE account_sync SET equity=?, margin=?, risk_pct=?, last_sync=? WHERE id=1",
                             (payload["equity"], payload["margin"], payload["risk_pct"], now_s))
            if "trade_update" in payload:
                tu = payload["trade_update"]
                conn.execute("""
                    UPDATE trades SET status=?, ticket=?, lot_size=?, pnl=?, 
                    entry=?, opened_at=COALESCE(opened_at, ?), closed_at=CASE WHEN ? IN ('WIN','LOSS','BE') THEN ? ELSE closed_at END
                    WHERE id=?
                """, (tu["status"], tu.get("ticket",0), tu.get("lot",0), tu.get("pnl",0), tu.get("entry",0), now_s, tu["status"], now_s, tu["id"]))
            conn.commit()
    return jsonify({"status": "synced"})

# --- UI DATA API (Z zaawansowaną matematyką) ---
@app.route("/api/ui_data", methods=["GET"])
def ui_data():
    with get_db() as conn:
        acc = dict(conn.execute("SELECT * FROM account_sync WHERE id=1").fetchone())
        settings = dict(conn.execute("SELECT * FROM settings WHERE id=1").fetchone())
        trades_rows = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 50").fetchall()
        trades = [dict(t) for t in trades_rows]
        
        now_utc = datetime.now(timezone.utc)
        last_sync_time = datetime.strptime(acc["last_sync"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        seconds_ago = (now_utc - last_sync_time).total_seconds()
        node_status = "ONLINE" if seconds_ago < 10 else "OFFLINE"
        
        wins = sum(1 for t in trades if t["status"] == "WIN")
        losses = sum(1 for t in trades if t["status"] == "LOSS")
        bes = sum(1 for t in trades if t["status"] == "BE")
        total_pnl = sum(t["pnl"] for t in trades if t["status"] in ["WIN", "LOSS", "BE"])
        
        # Wyliczenie Profit Factor (Zaawansowana analityka)
        gross_profit = sum(t["pnl"] for t in trades if t["status"] == "WIN")
        gross_loss = sum(abs(t["pnl"]) for t in trades if t["status"] == "LOSS")
        pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (round(gross_profit, 2) if gross_profit > 0 else 0)
        
        return jsonify({
            "acc": acc, "settings": settings, "trades": trades, 
            "node_status": node_status, "seconds_ago": seconds_ago,
            "stats": {"wins": wins, "losses": losses, "bes": bes, "total_pnl": total_pnl, "profit_factor": pf}
        })

@app.route("/api/toggle_bot", methods=["POST"])
def toggle_bot():
    with lock:
        with get_db() as conn:
            curr = conn.execute("SELECT bot_active FROM settings WHERE id=1").fetchone()["bot_active"]
            new_val = 1 if curr == 0 else 0
            conn.execute("UPDATE settings SET bot_active=? WHERE id=1", (new_val,))
            conn.commit()
    return jsonify({"status": "ok", "bot_active": new_val})

@app.route("/api/toggle_mode", methods=["POST"])
def toggle_mode():
    with lock:
        with get_db() as conn:
            curr = conn.execute("SELECT account_mode FROM settings WHERE id=1").fetchone()["account_mode"]
            new_mode = "LIVE" if curr == "DEMO" else "DEMO"
            conn.execute("UPDATE settings SET account_mode=? WHERE id=1", (new_mode,))
            conn.commit()
    return jsonify({"status": "ok", "mode": new_mode})

# --- NOWY INTERFEJS ENTERPRISE ---
DASHBOARD = """
<!DOCTYPE html>
<html lang="en" class="antialiased">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Apex Quant | Enterprise</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap');
        
        body { 
            font-family: 'Inter', sans-serif; 
            background-color: #09090b; /* Bardzo ciemny szary (zinc-950) */
            color: #fafafa;
            background-image: radial-gradient(circle at 50% 0%, rgba(39, 39, 42, 0.4) 0%, transparent 70%);
            background-attachment: fixed;
        }
        .mono { font-family: 'JetBrains Mono', monospace; }
        
        /* Panele typu Glassmorphism Premium */
        .premium-card {
            background: rgba(24, 24, 27, 0.6);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            border: 1px solid rgba(255, 255, 255, 0.08);
            box-shadow: 0 4px 30px rgba(0, 0, 0, 0.5);
            border-radius: 1.25rem;
        }
        
        /* Subtelne akcenty */
        .text-accent-green { color: #10b981; text-shadow: 0 0 20px rgba(16, 185, 129, 0.2); }
        .text-accent-red { color: #f43f5e; text-shadow: 0 0 20px rgba(244, 63, 94, 0.2); }
        .text-accent-gold { color: #fbbf24; text-shadow: 0 0 20px rgba(251, 191, 36, 0.2); }
        
        /* Przełączniki iOS Style */
        .toggle-checkbox:checked { right: 0; border-color: #10b981; }
        .toggle-checkbox:checked + .toggle-label { background-color: #10b981; }
        .toggle-checkbox:checked + .toggle-label:before { transform: translateX(100%); }
        
        .mode-checkbox:checked { right: 0; border-color: #f43f5e; }
        .mode-checkbox:checked + .mode-label { background-color: #f43f5e; }
        .mode-checkbox:checked + .mode-label:before { transform: translateX(100%); }
        
        .toggle-label { transition: background-color 0.3s ease; }
        .toggle-label:before { transition: transform 0.3s cubic-bezier(0.4, 0.0, 0.2, 1); }

        /* Pulsująca kropka online/offline */
        .pulse-dot { height: 8px; width: 8px; border-radius: 50%; display: inline-block; }
        .pulse-online { background-color: #10b981; box-shadow: 0 0 10px #10b981; animation: pulse-g 2s infinite; }
        .pulse-offline { background-color: #f43f5e; box-shadow: 0 0 10px #f43f5e; }
        @keyframes pulse-g { 0% { opacity: 1; transform: scale(1); } 50% { opacity: 0.5; transform: scale(1.2); } 100% { opacity: 1; transform: scale(1); } }
        
        /* Custom scrollbar dla tabeli */
        .custom-scrollbar::-webkit-scrollbar { height: 6px; width: 6px; }
        .custom-scrollbar::-webkit-scrollbar-track { background: rgba(0,0,0,0.2); border-radius: 10px; }
        .custom-scrollbar::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 10px; }
        .custom-scrollbar::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.2); }
    </style>
</head>
<body class="min-h-screen p-3 md:p-6 lg:p-8 flex flex-col gap-6">

    <!-- HEADER: Nawigacja i Zegary -->
    <header class="flex flex-col md:flex-row justify-between items-start md:items-center gap-4 premium-card p-5">
        <div class="flex items-center gap-4">
            <div class="w-12 h-12 rounded-xl bg-gradient-to-br from-zinc-700 to-zinc-900 flex items-center justify-center border border-white/10 shadow-lg">
                <i class="fa-solid fa-layer-group text-xl text-zinc-300"></i>
            </div>
            <div>
                <h1 class="text-xl md:text-2xl font-bold tracking-tight">Apex <span class="font-light text-zinc-400">Quant</span></h1>
                <div class="text-[10px] md:text-xs text-zinc-500 uppercase tracking-widest flex items-center gap-2 mt-1 font-medium">
                    <span id="node-dot" class="pulse-dot pulse-offline"></span>
                    <span id="node-status">System Offline</span>
                </div>
            </div>
        </div>
        
        <!-- Zegary Giełd (Widoczne tylko na większych ekranach) -->
        <div class="hidden lg:flex gap-6 border-l border-white/10 pl-6">
            <div class="flex flex-col">
                <span class="text-[10px] text-zinc-500 font-bold uppercase tracking-wider">London</span>
                <span class="mono text-sm" id="clock-ldn">--:--</span>
            </div>
            <div class="flex flex-col">
                <span class="text-[10px] text-zinc-500 font-bold uppercase tracking-wider">New York</span>
                <span class="mono text-sm" id="clock-ny">--:--</span>
            </div>
            <div class="flex flex-col">
                <span class="text-[10px] text-zinc-500 font-bold uppercase tracking-wider">Tokyo</span>
                <span class="mono text-sm" id="clock-tok">--:--</span>
            </div>
        </div>

        <!-- PRZEŁĄCZNIKI (MODE & KILL SWITCH) -->
        <div class="flex items-center gap-6 bg-zinc-900/80 p-3 rounded-xl border border-white/5 w-full md:w-auto justify-between md:justify-end">
            <!-- TRYB KANTA -->
            <div class="flex flex-col items-center">
                <span class="text-[9px] text-zinc-500 font-bold uppercase mb-1.5" id="mode-text">DEMO MODE</span>
                <div class="relative inline-block w-11 h-5 align-middle select-none">
                    <input type="checkbox" id="mode-toggle" class="mode-checkbox absolute block w-5 h-5 rounded-full bg-white border-4 border-zinc-700 appearance-none cursor-pointer z-10" onclick="toggleMode()"/>
                    <label for="mode-toggle" class="mode-label block overflow-hidden h-5 rounded-full bg-zinc-700 cursor-pointer"></label>
                </div>
            </div>
            
            <div class="w-px h-8 bg-white/10"></div>

            <!-- MASTER KILL SWITCH -->
            <div class="flex flex-col items-center">
                <span class="text-[9px] text-zinc-500 font-bold uppercase mb-1.5">BOT ENGINE</span>
                <div class="relative inline-block w-11 h-5 align-middle select-none">
                    <input type="checkbox" id="bot-toggle" class="toggle-checkbox absolute block w-5 h-5 rounded-full bg-white border-4 border-zinc-700 appearance-none cursor-pointer z-10" onclick="toggleBot()"/>
                    <label for="bot-toggle" class="toggle-label block overflow-hidden h-5 rounded-full bg-zinc-700 cursor-pointer"></label>
                </div>
            </div>
        </div>
    </header>

    <!-- GŁÓWNE STATYSTYKI -->
    <div class="grid grid-cols-2 lg:grid-cols-4 gap-3 md:gap-6">
        <!-- Equity -->
        <div class="premium-card p-5 relative overflow-hidden group">
            <div class="absolute -right-4 -top-4 w-24 h-24 bg-emerald-500/10 rounded-full blur-2xl group-hover:bg-emerald-500/20 transition-all"></div>
            <h3 class="text-[10px] md:text-xs text-zinc-400 font-semibold uppercase tracking-wider mb-2 flex items-center justify-between">
                Live Equity <i class="fa-solid fa-wallet text-zinc-600"></i>
            </h3>
            <div class="mono text-2xl md:text-4xl font-bold text-accent-green" id="ui-equity">$0.00</div>
            <div class="text-[9px] md:text-xs text-zinc-500 mt-2 mono">MT5 Sync</div>
        </div>
        
        <!-- P&L -->
        <div class="premium-card p-5 relative overflow-hidden">
            <h3 class="text-[10px] md:text-xs text-zinc-400 font-semibold uppercase tracking-wider mb-2 flex items-center justify-between">
                Net P&L <i class="fa-solid fa-chart-line text-zinc-600"></i>
            </h3>
            <div class="mono text-2xl md:text-4xl font-bold" id="ui-pnl">$0.00</div>
            <div class="text-[9px] md:text-xs text-zinc-500 mt-2 mono">Session Total</div>
        </div>

        <!-- Profit Factor -->
        <div class="premium-card p-5">
            <h3 class="text-[10px] md:text-xs text-zinc-400 font-semibold uppercase tracking-wider mb-2 flex items-center justify-between">
                Profit Factor <i class="fa-solid fa-scale-balanced text-zinc-600"></i>
            </h3>
            <div class="mono text-2xl md:text-4xl font-bold text-zinc-100" id="ui-pf">0.00</div>
            <div class="text-[9px] md:text-xs text-zinc-500 mt-2 mono">Gross Win / Gross Loss</div>
        </div>
        
        <!-- Kelly Risk -->
        <div class="premium-card p-5">
            <h3 class="text-[10px] md:text-xs text-zinc-400 font-semibold uppercase tracking-wider mb-2 flex items-center justify-between">
                Kelly Target <i class="fa-solid fa-crosshairs text-zinc-600"></i>
            </h3>
            <div class="mono text-2xl md:text-4xl font-bold text-accent-gold" id="ui-risk">0.0%</div>
            <div class="text-[9px] md:text-xs text-zinc-500 mt-2 mono">Auto-Scaled Risk</div>
        </div>
    </div>

    <!-- SEKCJA DOLNA: Wykres i Logi -->
    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6 flex-grow">
        <!-- WYKRES KAPITAŁU -->
        <div class="premium-card p-5 lg:col-span-1 flex flex-col h-[300px] lg:h-auto">
            <div class="flex justify-between items-center mb-4">
                <h3 class="text-xs text-zinc-400 font-semibold uppercase tracking-wider">Equity Curve</h3>
                <span class="text-[10px] font-bold text-zinc-500" id="ui-winrate">WR: 0%</span>
            </div>
            <div class="flex-grow relative w-full">
                <canvas id="pnlChart"></canvas>
            </div>
            <!-- Mały pasek W/L/BE pod wykresem -->
            <div class="w-full h-1.5 bg-zinc-800 rounded-full overflow-hidden flex mt-4" id="win-bar">
                <div class="h-full bg-emerald-500 w-1/3"></div>
                <div class="h-full bg-zinc-500 w-1/3"></div>
                <div class="h-full bg-rose-500 w-1/3"></div>
            </div>
            <div class="flex justify-between mt-1 px-1">
                <span class="text-[9px] font-bold text-emerald-500" id="ui-wins">0 W</span>
                <span class="text-[9px] font-bold text-zinc-500" id="ui-bes">0 BE</span>
                <span class="text-[9px] font-bold text-rose-500" id="ui-losses">0 L</span>
            </div>
        </div>

        <!-- TABELA TRANSAKCJI -->
        <div class="premium-card p-5 lg:col-span-2 flex flex-col overflow-hidden">
            <div class="flex justify-between items-center mb-4">
                <h3 class="text-xs text-zinc-400 font-semibold uppercase tracking-wider">Algorithmic Ledger</h3>
                <span class="text-[9px] text-zinc-400 bg-zinc-800/50 px-2 py-1 rounded-md border border-white/5 uppercase tracking-widest flex items-center gap-2">
                    <span class="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse"></span> Live
                </span>
            </div>
            <!-- Wraper z włączonym scrollowaniem (Mobilność) -->
            <div class="overflow-x-auto overflow-y-auto max-h-[400px] custom-scrollbar flex-grow">
                <table class="w-full text-left border-collapse whitespace-nowrap min-w-[600px]">
                    <thead class="sticky top-0 bg-[#18181b] z-10 shadow-sm">
                        <tr>
                            <th class="p-3 text-[10px] uppercase text-zinc-500 font-semibold border-b border-white/5">Ticket</th>
                            <th class="p-3 text-[10px] uppercase text-zinc-500 font-semibold border-b border-white/5">Side</th>
                            <th class="p-3 text-[10px] uppercase text-zinc-500 font-semibold border-b border-white/5">Entry / Size</th>
                            <th class="p-3 text-[10px] uppercase text-zinc-500 font-semibold border-b border-white/5">Status</th>
                            <th class="p-3 text-[10px] uppercase text-zinc-500 font-semibold border-b border-white/5 text-right">Net P&L</th>
                            <th class="p-3 text-[10px] uppercase text-zinc-500 font-semibold border-b border-white/5 text-right">Time (UTC)</th>
                        </tr>
                    </thead>
                    <tbody id="trades-body" class="text-xs mono">
                        <!-- Generowane przez JS -->
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- JAVASCRIPT -->
    <script>
        // --- Zegary Świata ---
        function updateClocks() {
            const now = new Date();
            const fmt = (tz) => now.toLocaleTimeString('en-US', { timeZone: tz, hour12: false, hour: '2-digit', minute:'2-digit' });
            document.getElementById('clock-ldn').innerText = fmt('Europe/London');
            document.getElementById('clock-ny').innerText = fmt('America/New_York');
            document.getElementById('clock-tok').innerText = fmt('Asia/Tokyo');
        }
        setInterval(updateClocks, 1000);
        updateClocks();

        // --- Konfiguracja Wykresu (Stylizowana) ---
        const ctx = document.getElementById('pnlChart').getContext('2d');
        let gradient = ctx.createLinearGradient(0, 0, 0, 300);
        gradient.addColorStop(0, 'rgba(16, 185, 129, 0.4)'); // Emerald
        gradient.addColorStop(1, 'rgba(16, 185, 129, 0.0)');
        
        Chart.defaults.color = '#71717a';
        Chart.defaults.font.family = "'JetBrains Mono', monospace";
        const pnlChart = new Chart(ctx, {
            type: 'line',
            data: { labels: [], datasets: [{ 
                data: [], 
                borderColor: '#10b981', 
                backgroundColor: gradient, 
                borderWidth: 2, 
                fill: true, 
                tension: 0.4, 
                pointBackgroundColor: '#09090b',
                pointBorderColor: '#10b981',
                pointBorderWidth: 2,
                pointRadius: 3,
                pointHoverRadius: 5
            }]},
            options: { 
                responsive: true, 
                maintainAspectRatio: false, 
                plugins: { legend: { display: false } }, 
                scales: { 
                    x: { display: false }, 
                    y: { 
                        grid: { color: 'rgba(255,255,255,0.03)', drawBorder: false },
                        ticks: { callback: function(value) { return '$' + value; }, font: {size: 10} }
                    } 
                } 
            }
        });

        // --- API Calls ---
        async function toggleBot() { await fetch('/api/toggle_bot', { method: 'POST' }); fetchData(); }
        async function toggleMode() { await fetch('/api/toggle_mode', { method: 'POST' }); fetchData(); }
        function formatMoney(amount) { return amount >= 0 ? "+$" + amount.toFixed(2) : "-$" + Math.abs(amount).toFixed(2); }

        async function fetchData() {
            try {
                const res = await fetch('/api/ui_data');
                const data = await res.json();
                
                // Kontrolki Switch
                document.getElementById('bot-toggle').checked = data.settings.bot_active === 1;
                document.getElementById('mode-toggle').checked = data.settings.account_mode === 'LIVE';
                
                let modeText = document.getElementById('mode-text');
                modeText.innerText = data.settings.account_mode + " ACCOUNT";
                modeText.className = data.settings.account_mode === 'LIVE' ? "text-[9px] font-bold uppercase mb-1.5 text-rose-500" : "text-[9px] font-bold uppercase mb-1.5 text-zinc-500";

                // Główne Kafelki
                document.getElementById('ui-equity').innerText = "$" + data.acc.equity.toFixed(2);
                document.getElementById('ui-risk').innerText = data.acc.risk_pct.toFixed(1) + "%";
                document.getElementById('ui-pf').innerText = data.stats.profit_factor.toFixed(2);
                
                let pnlEl = document.getElementById('ui-pnl');
                pnlEl.innerText = formatMoney(data.stats.total_pnl);
                pnlEl.className = data.stats.total_pnl >= 0 ? "mono text-2xl md:text-4xl font-bold text-accent-green" : "mono text-2xl md:text-4xl font-bold text-accent-red";

                // Statystyki Paska
                document.getElementById('ui-wins').innerText = data.stats.wins + " W";
                document.getElementById('ui-losses').innerText = data.stats.losses + " L";
                document.getElementById('ui-bes').innerText = data.stats.bes + " BE";
                
                let totalFinished = data.stats.wins + data.stats.losses; 
                let wr = totalFinished > 0 ? Math.round((data.stats.wins / totalFinished) * 100) : 0;
                document.getElementById('ui-winrate').innerText = "WR: " + wr + "%";
                
                let totalAll = data.stats.wins + data.stats.losses + data.stats.bes;
                if(totalAll > 0) {
                    let wPct = (data.stats.wins / totalAll) * 100;
                    let bPct = (data.stats.bes / totalAll) * 100;
                    let lPct = (data.stats.losses / totalAll) * 100;
                    document.getElementById('win-bar').innerHTML = `
                        <div class="h-full bg-emerald-500" style="width: ${wPct}%"></div>
                        <div class="h-full bg-zinc-600" style="width: ${bPct}%"></div>
                        <div class="h-full bg-rose-500" style="width: ${lPct}%"></div>
                    `;
                }

                // Node Status (Ping & Safety)
                let dot = document.getElementById('node-dot');
                let txt = document.getElementById('node-status');
                if(data.node_status === "ONLINE") {
                    dot.className = "pulse-dot pulse-online";
                    txt.innerText = "System Online (" + Math.round(data.seconds_ago) + "s ping)";
                    txt.className = "text-emerald-400 font-bold";
                } else {
                    dot.className = "pulse-dot pulse-offline";
                    txt.innerText = "Connection Lost (Check MT5)";
                    txt.className = "text-rose-400 font-bold";
                }

                // Tabela
                let tbody = document.getElementById('trades-body');
                tbody.innerHTML = '';
                let chartData = []; let chartLabels = []; let cumPnl = 0;
                
                data.trades.forEach(t => {
                    let badgeClass = t.status === 'WIN' ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20' : 
                                     t.status === 'LOSS' ? 'bg-rose-500/10 text-rose-400 border-rose-500/20' : 
                                     t.status === 'BE' ? 'bg-zinc-500/10 text-zinc-400 border-zinc-500/20' : 
                                     t.status === 'PENDING' ? 'bg-amber-500/10 text-amber-400 border-amber-500/20' : 'bg-white/5 text-zinc-500 border-white/5';
                    
                    let pnlStr = t.status === 'PENDING' ? '---' : formatMoney(t.pnl);
                    let pnlColor = t.pnl > 0 ? 'text-emerald-400' : t.pnl < 0 ? 'text-rose-400' : 'text-zinc-500';
                    let actionColor = t.action === 'LONG' ? 'text-emerald-500' : 'text-rose-500';
                    
                    let tr = document.createElement('tr');
                    tr.className = "hover:bg-white/5 transition-colors border-b border-white/5 last:border-0";
                    tr.innerHTML = `
                        <td class="p-3 text-zinc-500">#${t.ticket || 'AWAIT'}</td>
                        <td class="p-3 font-bold ${actionColor}">${t.action}</td>
                        <td class="p-3 text-zinc-300">@${t.entry.toFixed(2)} <span class="text-zinc-600 ml-1">(${t.lot_size.toFixed(2)}L)</span></td>
                        <td class="p-3"><span class="px-2 py-0.5 rounded text-[9px] font-bold border ${badgeClass}">${t.status}</span></td>
                        <td class="p-3 font-bold text-right ${pnlColor}">${pnlStr}</td>
                        <td class="p-3 text-zinc-500 text-right">${t.opened_at ? t.opened_at.split(' ')[1] : '---'}</td>
                    `;
                    tbody.appendChild(tr);
                });

                // Rysowanie wykresu
                let finishedTrades = data.trades.filter(t => ['WIN', 'LOSS', 'BE'].includes(t.status)).reverse();
                chartLabels = ['Start']; chartData = [0];
                finishedTrades.forEach((t, i) => {
                    cumPnl += t.pnl;
                    chartLabels.push('#' + (i+1));
                    chartData.push(cumPnl);
                });
                
                // Zmień kolor wykresu na czerwony jeśli kapitał jest na minusie względem startu
                if(cumPnl < 0) {
                    let redGrad = ctx.createLinearGradient(0, 0, 0, 300);
                    redGrad.addColorStop(0, 'rgba(244, 63, 94, 0.4)');
                    redGrad.addColorStop(1, 'rgba(244, 63, 94, 0.0)');
                    pnlChart.data.datasets[0].borderColor = '#f43f5e';
                    pnlChart.data.datasets[0].backgroundColor = redGrad;
                    pnlChart.data.datasets[0].pointBorderColor = '#f43f5e';
                } else {
                    pnlChart.data.datasets[0].borderColor = '#10b981';
                    pnlChart.data.datasets[0].backgroundColor = gradient;
                    pnlChart.data.datasets[0].pointBorderColor = '#10b981';
                }

                pnlChart.data.labels = chartLabels;
                pnlChart.data.datasets[0].data = chartData;
                pnlChart.update();

            } catch (error) { console.error("Sync error", error); }
        }

        fetchData();
        setInterval(fetchData, 1500);
    </script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(DASHBOARD)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
