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
        # Nowa tabela: Ustawienia Systemowe (Kontrola z poziomu UI)
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

# --- MT5 ENGINE API (Komunikacja z komputerem) ---
@app.route("/api/pending", methods=["GET"])
def get_pending():
    secret = request.args.get("secret")
    if secret != WEBHOOK_SECRET: return jsonify({"error": "Unauthorized"}), 401
    
    with get_db() as conn:
        # SPRAWDZENIE KILL-SWITCHA: Jeśli bot_active == 0, MT5 nie dostanie sygnału!
        settings = conn.execute("SELECT bot_active FROM settings WHERE id=1").fetchone()
        if not settings or settings["bot_active"] == 0:
            return jsonify({}) # Zwróć pusto, bot jest wyłączony z poziomu WWW
            
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

# --- API DLA NOWEGO INTERFEJSU (AJAX) ---
@app.route("/api/ui_data", methods=["GET"])
def ui_data():
    with get_db() as conn:
        acc = dict(conn.execute("SELECT * FROM account_sync WHERE id=1").fetchone())
        settings = dict(conn.execute("SELECT * FROM settings WHERE id=1").fetchone())
        trades_rows = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 50").fetchall()
        trades = [dict(t) for t in trades_rows]
        
        # Obliczenie opóźnienia węzła (Ping)
        now_utc = datetime.now(timezone.utc)
        last_sync_time = datetime.strptime(acc["last_sync"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        seconds_ago = (now_utc - last_sync_time).total_seconds()
        node_status = "ONLINE" if seconds_ago < 10 else "OFFLINE"
        
        # Statystyki
        wins = sum(1 for t in trades if t["status"] == "WIN")
        losses = sum(1 for t in trades if t["status"] == "LOSS")
        bes = sum(1 for t in trades if t["status"] == "BE")
        total_pnl = sum(t["pnl"] for t in trades if t["status"] in ["WIN", "LOSS", "BE"])
        
        return jsonify({
            "acc": acc, "settings": settings, "trades": trades, 
            "node_status": node_status, "seconds_ago": seconds_ago,
            "stats": {"wins": wins, "losses": losses, "bes": bes, "total_pnl": total_pnl}
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

# --- NOWY INTERFEJS GRAFICZNY (HTML/CSS/JS) ---
DASHBOARD = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Apex Quant | Terminal</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700&family=JetBrains+Mono:wght@400;700;800&display=swap');
        body { font-family: 'Inter', sans-serif; background-color: #050505; color: #e5e5e5; }
        .mono { font-family: 'JetBrains Mono', monospace; }
        .glass { background: rgba(15, 15, 18, 0.7); backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.05); }
        .neon-green { color: #00ff88; text-shadow: 0 0 10px rgba(0,255,136,0.3); }
        .neon-red { color: #ff3366; text-shadow: 0 0 10px rgba(255,51,102,0.3); }
        .neon-gold { color: #ffd700; text-shadow: 0 0 10px rgba(255,215,0,0.3); }
        
        /* Toggle Switch CSS */
        .toggle-checkbox:checked { right: 0; border-color: #00ff88; }
        .toggle-checkbox:checked + .toggle-label { background-color: #00ff88; }
        .toggle-checkbox:checked + .toggle-label:before { transform: translateX(100%); }
        
        .mode-checkbox:checked { right: 0; }
        .mode-checkbox:checked + .mode-label { background-color: #ff3366; }
        .mode-checkbox:checked + .mode-label:before { transform: translateX(100%); }

        .pulse-dot { height: 10px; width: 10px; border-radius: 50%; display: inline-block; }
        .pulse-online { background-color: #00ff88; box-shadow: 0 0 12px #00ff88; animation: pulse-g 2s infinite; }
        .pulse-offline { background-color: #ff3366; box-shadow: 0 0 12px #ff3366; animation: pulse-r 2s infinite; }
        @keyframes pulse-g { 0% { opacity: 1; } 50% { opacity: 0.4; } 100% { opacity: 1; } }
        @keyframes pulse-r { 0% { opacity: 1; } 50% { opacity: 0.4; } 100% { opacity: 1; } }
        
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: #0a0a0a; }
        ::-webkit-scrollbar-thumb { background: #333; border-radius: 3px; }
    </style>
</head>
<body class="min-h-screen p-4 md:p-8 flex flex-col gap-6">

    <!-- HEADER / NAVIGATION -->
    <header class="glass rounded-2xl p-4 flex flex-col md:flex-row justify-between items-center gap-4">
        <div class="flex items-center gap-4">
            <i class="fa-solid fa-bolt text-3xl neon-gold"></i>
            <div>
                <h1 class="text-2xl font-bold tracking-tight">APEX <span class="font-light">QUANT</span></h1>
                <div class="text-xs text-gray-500 uppercase tracking-widest flex items-center gap-2 mt-1">
                    <span id="node-dot" class="pulse-dot pulse-offline"></span>
                    <span id="node-status">NODE OFFLINE (Awaiting Sync)</span>
                </div>
            </div>
        </div>

        <!-- CONTROLS -->
        <div class="flex items-center gap-8 bg-black/50 p-3 rounded-xl border border-white/5">
            <!-- MODE TOGGLE -->
            <div class="flex flex-col items-center">
                <span class="text-[10px] text-gray-400 font-bold uppercase mb-1" id="mode-text">DEMO MODE</span>
                <div class="relative inline-block w-12 h-6 align-middle select-none transition duration-200 ease-in">
                    <input type="checkbox" id="mode-toggle" class="mode-checkbox absolute block w-6 h-6 rounded-full bg-white border-4 border-gray-700 appearance-none cursor-pointer transition-transform duration-200 ease-in-out z-10" onclick="toggleMode()"/>
                    <label for="mode-toggle" class="mode-label block overflow-hidden h-6 rounded-full bg-gray-700 cursor-pointer"></label>
                </div>
            </div>
            
            <div class="w-px h-8 bg-white/10"></div>

            <!-- BOT MASTER SWITCH -->
            <div class="flex flex-col items-center">
                <span class="text-[10px] text-gray-400 font-bold uppercase mb-1">MASTER SWITCH</span>
                <div class="relative inline-block w-12 h-6 align-middle select-none transition duration-200 ease-in">
                    <input type="checkbox" id="bot-toggle" class="toggle-checkbox absolute block w-6 h-6 rounded-full bg-white border-4 border-gray-700 appearance-none cursor-pointer transition-transform duration-200 ease-in-out z-10" onclick="toggleBot()"/>
                    <label for="bot-toggle" class="toggle-label block overflow-hidden h-6 rounded-full bg-gray-700 cursor-pointer"></label>
                </div>
            </div>
        </div>
    </header>

    <!-- METRICS GRID -->
    <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
        <div class="glass p-6 rounded-2xl relative overflow-hidden">
            <div class="absolute top-0 right-0 w-32 h-32 bg-green-500/5 rounded-full blur-2xl -mr-10 -mt-10"></div>
            <h3 class="text-xs text-gray-400 font-semibold uppercase tracking-wider mb-2">Live Equity</h3>
            <div class="mono text-4xl font-bold neon-green" id="ui-equity">$0.00</div>
            <div class="text-xs text-gray-500 mt-2">Sourced from MT5 Terminal</div>
        </div>
        
        <div class="glass p-6 rounded-2xl relative overflow-hidden">
            <div class="absolute top-0 right-0 w-32 h-32 bg-yellow-500/5 rounded-full blur-2xl -mr-10 -mt-10"></div>
            <h3 class="text-xs text-gray-400 font-semibold uppercase tracking-wider mb-2">Kelly Target Risk</h3>
            <div class="mono text-4xl font-bold neon-gold" id="ui-risk">0.0%</div>
            <div class="text-xs text-gray-500 mt-2">Auto-scaled per trade</div>
        </div>
        
        <div class="glass p-6 rounded-2xl">
            <h3 class="text-xs text-gray-400 font-semibold uppercase tracking-wider mb-2">Total Net P&L</h3>
            <div class="mono text-4xl font-bold" id="ui-pnl">$0.00</div>
            <div class="text-xs text-gray-500 mt-2">Session profit</div>
        </div>
        
        <div class="glass p-6 rounded-2xl flex flex-col justify-center">
            <h3 class="text-xs text-gray-400 font-semibold uppercase tracking-wider mb-4">Performance</h3>
            <div class="flex justify-between items-end mb-1">
                <span class="text-sm font-bold text-green-400" id="ui-wins">0 W</span>
                <span class="text-sm font-bold text-gray-400" id="ui-bes">0 BE</span>
                <span class="text-sm font-bold text-red-400" id="ui-losses">0 L</span>
            </div>
            <div class="w-full h-2 bg-gray-800 rounded-full overflow-hidden flex" id="win-bar">
                <div class="h-full bg-green-500 w-1/3"></div>
                <div class="h-full bg-gray-500 w-1/3"></div>
                <div class="h-full bg-red-500 w-1/3"></div>
            </div>
            <div class="text-center mt-2 mono text-xs text-gray-400" id="ui-winrate">WR: 0%</div>
        </div>
    </div>

    <!-- MIDDLE SECTION: CHART & LOGS -->
    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6 flex-grow">
        <!-- CHART -->
        <div class="glass rounded-2xl p-6 lg:col-span-1 flex flex-col">
            <h3 class="text-xs text-gray-400 font-semibold uppercase tracking-wider mb-4">Equity Curve (Session)</h3>
            <div class="flex-grow relative w-full h-64">
                <canvas id="pnlChart"></canvas>
            </div>
        </div>

        <!-- EXECUTION LOG -->
        <div class="glass rounded-2xl p-6 lg:col-span-2 flex flex-col">
            <div class="flex justify-between items-center mb-4">
                <h3 class="text-xs text-gray-400 font-semibold uppercase tracking-wider">Algorithmic Execution Log</h3>
                <span class="text-xs text-gray-500 bg-black/40 px-3 py-1 rounded-full"><i class="fa-solid fa-rotate mr-2 fa-spin" style="animation-duration: 3s;"></i>Live Sync</span>
            </div>
            <div class="overflow-x-auto overflow-y-auto max-h-[300px]">
                <table class="w-full text-left border-collapse">
                    <thead class="sticky top-0 bg-[#0f0f12]">
                        <tr>
                            <th class="p-3 text-[10px] uppercase text-gray-500 font-bold border-b border-white/5">Ticket</th>
                            <th class="p-3 text-[10px] uppercase text-gray-500 font-bold border-b border-white/5">Action</th>
                            <th class="p-3 text-[10px] uppercase text-gray-500 font-bold border-b border-white/5">Entry / Size</th>
                            <th class="p-3 text-[10px] uppercase text-gray-500 font-bold border-b border-white/5">Status</th>
                            <th class="p-3 text-[10px] uppercase text-gray-500 font-bold border-b border-white/5">Net P&L</th>
                            <th class="p-3 text-[10px] uppercase text-gray-500 font-bold border-b border-white/5 text-right">Timestamp</th>
                        </tr>
                    </thead>
                    <tbody id="trades-body" class="text-xs mono">
                        <!-- Trzeba wygenerowane przez JS -->
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- JAVASCRIPT LOGIC (AJAX & UI UPDATES) -->
    <script>
        // Setup Chart.js
        const ctx = document.getElementById('pnlChart').getContext('2d');
        Chart.defaults.color = '#666';
        Chart.defaults.font.family = "'JetBrains Mono', monospace";
        const pnlChart = new Chart(ctx, {
            type: 'line',
            data: { labels: [], datasets: [{ label: 'Cumulative P&L', data: [], borderColor: '#00ff88', backgroundColor: 'rgba(0, 255, 136, 0.1)', borderWidth: 2, fill: true, tension: 0.4, pointRadius: 0 }] },
            options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { display: false }, y: { grid: { color: 'rgba(255,255,255,0.05)' } } } }
        });

        async function toggleBot() { await fetch('/api/toggle_bot', { method: 'POST' }); fetchData(); }
        async function toggleMode() { await fetch('/api/toggle_mode', { method: 'POST' }); fetchData(); }

        function formatMoney(amount) { return amount >= 0 ? "+$" + amount.toFixed(2) : "-$" + Math.abs(amount).toFixed(2); }

        async function fetchData() {
            try {
                const res = await fetch('/api/ui_data');
                const data = await res.json();
                
                // Zaktualizuj kontrolki (Toggles)
                document.getElementById('bot-toggle').checked = data.settings.bot_active === 1;
                document.getElementById('mode-toggle').checked = data.settings.account_mode === 'LIVE';
                document.getElementById('mode-text').innerText = data.settings.account_mode + " MODE";
                document.getElementById('mode-text').className = data.settings.account_mode === 'LIVE' ? "text-[10px] font-bold uppercase mb-1 text-red-400" : "text-[10px] font-bold uppercase mb-1 text-blue-400";

                // Zaktualizuj górne karty
                document.getElementById('ui-equity').innerText = "$" + data.acc.equity.toFixed(2);
                document.getElementById('ui-risk').innerText = data.acc.risk_pct.toFixed(1) + "%";
                
                let pnlEl = document.getElementById('ui-pnl');
                pnlEl.innerText = formatMoney(data.stats.total_pnl);
                pnlEl.className = data.stats.total_pnl >= 0 ? "mono text-4xl font-bold neon-green" : "mono text-4xl font-bold neon-red";

                // Statystyki
                document.getElementById('ui-wins').innerText = data.stats.wins + " W";
                document.getElementById('ui-losses').innerText = data.stats.losses + " L";
                document.getElementById('ui-bes').innerText = data.stats.bes + " BE";
                
                let totalFinished = data.stats.wins + data.stats.losses; // Winrate bez BE
                let wr = totalFinished > 0 ? Math.round((data.stats.wins / totalFinished) * 100) : 0;
                document.getElementById('ui-winrate').innerText = "WR: " + wr + "%";
                
                let totalAll = data.stats.wins + data.stats.losses + data.stats.bes;
                if(totalAll > 0) {
                    let wPct = (data.stats.wins / totalAll) * 100;
                    let bPct = (data.stats.bes / totalAll) * 100;
                    let lPct = (data.stats.losses / totalAll) * 100;
                    document.getElementById('win-bar').innerHTML = `
                        <div class="h-full bg-green-500" style="width: ${wPct}%"></div>
                        <div class="h-full bg-gray-500" style="width: ${bPct}%"></div>
                        <div class="h-full bg-red-500" style="width: ${lPct}%"></div>
                    `;
                }

                // Node Status
                let dot = document.getElementById('node-dot');
                let txt = document.getElementById('node-status');
                if(data.node_status === "ONLINE") {
                    dot.className = "pulse-dot pulse-online";
                    txt.innerText = "NODE ONLINE (" + Math.round(data.seconds_ago) + "s ping)";
                    txt.className = "text-green-400";
                } else {
                    dot.className = "pulse-dot pulse-offline";
                    txt.innerText = "NODE OFFLINE (Check MT5)";
                    txt.className = "text-red-400";
                }

                // Tabela
                let tbody = document.getElementById('trades-body');
                tbody.innerHTML = '';
                let chartData = []; let chartLabels = []; let cumPnl = 0;
                
                // Rysuj tabele
                data.trades.forEach(t => {
                    let badgeClass = t.status === 'WIN' ? 'bg-green-500/20 text-green-400' : 
                                     t.status === 'LOSS' ? 'bg-red-500/20 text-red-400' : 
                                     t.status === 'BE' ? 'bg-gray-500/20 text-gray-400' : 
                                     t.status === 'PENDING' ? 'bg-yellow-500/20 text-yellow-400' : 'bg-white/5 text-gray-500';
                    
                    let pnlStr = t.status === 'PENDING' ? '---' : formatMoney(t.pnl);
                    let pnlColor = t.pnl > 0 ? 'text-green-400' : t.pnl < 0 ? 'text-red-400' : 'text-gray-400';
                    
                    let tr = document.createElement('tr');
                    tr.className = "hover:bg-white/5 transition-colors border-b border-white/5 last:border-0";
                    tr.innerHTML = `
                        <td class="p-3 text-gray-500">#${t.ticket || 'WAIT'}</td>
                        <td class="p-3 font-bold ${t.action === 'LONG' ? 'text-green-400' : 'text-red-400'}">${t.action}</td>
                        <td class="p-3 text-gray-300">@${t.entry.toFixed(2)} <span class="text-gray-600 ml-1">(${t.lot_size.toFixed(2)}L)</span></td>
                        <td class="p-3"><span class="px-2 py-1 rounded text-[10px] font-bold ${badgeClass}">${t.status}</span></td>
                        <td class="p-3 font-bold ${pnlColor}">${pnlStr}</td>
                        <td class="p-3 text-gray-500 text-right">${t.opened_at ? t.opened_at.split(' ')[1] : '---'}</td>
                    `;
                    tbody.appendChild(tr);
                });

                // Aktualizacja wykresu (odwrotna kolejność dla osi czasu)
                let finishedTrades = data.trades.filter(t => ['WIN', 'LOSS', 'BE'].includes(t.status)).reverse();
                chartLabels = ['Start']; chartData = [0];
                finishedTrades.forEach((t, i) => {
                    cumPnl += t.pnl;
                    chartLabels.push('#' + (i+1));
                    chartData.push(cumPnl);
                });
                pnlChart.data.labels = chartLabels;
                pnlChart.data.datasets[0].data = chartData;
                pnlChart.update();

            } catch (error) { console.error("Sync error", error); }
        }

        // Uruchomienie asynchronicznej pętli (Odświeżanie co 1.5 sekundy)
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
