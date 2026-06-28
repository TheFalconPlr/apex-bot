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
        conn.execute("INSERT OR IGNORE INTO account_sync (id, equity) VALUES (1, 0)")
        conn.commit()

# WYMUSZENIE STARTU BAZY DANYCH DLA SERWERÓW CLOUD (Naprawia Błąd 500)
init_db()

# --- TRADINGVIEW WEBHOOK (Odbiór sygnału z wykresu) ---
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify({"error": "Unauthorized"}), 401
            
        action, entry, sl, tp1 = data["action"], float(data["entry"]), float(data["sl"]), float(data["tp1"])
        
        with lock:
            with get_db() as conn:
                # Anuluj stare, niewykonane sygnały
                conn.execute("UPDATE trades SET status='CANCELED' WHERE status='PENDING'")
                # Zapisz nowy sygnał
                conn.execute("INSERT INTO trades (action, entry, sl, tp1, status) VALUES (?,?,?,?,'PENDING')",
                             (action, entry, sl, tp1))
                conn.commit()
        return jsonify({"status": "signal_received_and_routed"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- MT5 ENGINE API (Komunikacja z lokalnym Pythonem na PC) ---
@app.route("/api/pending", methods=["GET"])
def get_pending():
    secret = request.args.get("secret")
    if secret != WEBHOOK_SECRET: return jsonify({"error": "Unauthorized"}), 401
    with get_db() as conn:
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
            # Sync parametrów konta (Saldo z MT5)
            if "equity" in payload:
                conn.execute("UPDATE account_sync SET equity=?, margin=?, risk_pct=?, last_sync=? WHERE id=1",
                             (payload["equity"], payload["margin"], payload["risk_pct"], now_s))
            # Aktualizacja statusu transakcji po zrealizowaniu przez MT5
            if "trade_update" in payload:
                tu = payload["trade_update"]
                conn.execute("""
                    UPDATE trades SET status=?, ticket=?, lot_size=?, pnl=?, 
                    entry=?, opened_at=COALESCE(opened_at, ?), closed_at=CASE WHEN ? IN ('WIN','LOSS','BE') THEN ? ELSE closed_at END
                    WHERE id=?
                """, (tu["status"], tu.get("ticket",0), tu.get("lot",0), tu.get("pnl",0), tu.get("entry",0), now_s, tu["status"], now_s, tu["id"]))
            conn.commit()
    return jsonify({"status": "synced"})

# --- DASHBOARD UI ---
DASHBOARD = """
<!DOCTYPE html>
<html lang="en">
<head>
<title>Apex Quant | Control Center</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&family=JetBrains+Mono:wght@700&display=swap" rel="stylesheet">
<style>
  :root { --bg:#050505; --card:#111111; --text:#ededed; --win:#00d26a; --loss:#f92f60; --be:#8e8e93; --accent:#eab308; }
  body { background:var(--bg); color:var(--text); font-family:'Inter', sans-serif; padding:40px; margin:0; }
  .grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap:20px; }
  .card { background:var(--card); padding:24px; border-radius:12px; border:1px solid #222; }
  h1 { font-family:'JetBrains Mono', monospace; font-size:24px; color:var(--accent); margin:0 0 30px 0; letter-spacing: -1px;}
  h2 { font-size:11px; text-transform:uppercase; color:#666; letter-spacing:1px; margin:0 0 10px 0; }
  .val { font-family:'JetBrains Mono', monospace; font-size:32px; font-weight:700; margin:5px 0; }
  .pos { color:var(--win); } .neg { color:var(--loss); } .neu { color:var(--be); }
  table { width:100%; border-collapse:collapse; margin-top:20px; font-size:13px; }
  th, td { text-align:left; padding:12px; border-bottom:1px solid #222; }
  th { color:#666; font-size:11px; text-transform:uppercase; }
  .badge { padding:4px 8px; border-radius:4px; font-size:10px; font-weight:800; }
  .badge-OPEN { background:rgba(234, 179, 8, 0.1); color:var(--accent); }
  .badge-WIN { background:rgba(0, 210, 106, 0.1); color:var(--win); }
  .badge-LOSS { background:rgba(249, 47, 96, 0.1); color:var(--loss); }
  .badge-BE { background:rgba(142, 142, 147, 0.1); color:var(--be); }
  .badge-PENDING { background:rgba(255, 255, 255, 0.1); color:#fff; }
  .badge-CANCELED { background:rgba(255, 255, 255, 0.05); color:#666; }
  .pulse { display:inline-block; width:8px; height:8px; background:var(--win); border-radius:50%; box-shadow:0 0 10px var(--win); margin-right:8px; animation:blink 2s infinite; }
  @keyframes blink { 50% { opacity:0.3; } }
</style>
</head>
<body>
  <h1><span class="pulse"></span>APEX QUANT_NODE</h1>
  <div class="grid">
    <div class="card"><h2>Live MT5 Equity</h2><div class="val pos">${{ "%.2f"|format(acc.equity) }}</div></div>
    <div class="card"><h2>Current Kelly Risk</h2><div class="val" style="color:var(--accent)">{{ "%.1f"|format(acc.risk_pct) }}%</div></div>
    <div class="card"><h2>Active Margin</h2><div class="val neu">${{ "%.2f"|format(acc.margin) }}</div></div>
    <div class="card">
        <h2>System Stats</h2>
        <div style="display:flex; justify-content:space-between; margin-top:10px;">
            <div><span style="color:#666; font-size:12px;">WINS</span><br><span class="pos" style="font-weight:700; font-size:18px;">{{ wins }}</span></div>
            <div><span style="color:#666; font-size:12px;">LOSSES</span><br><span class="neg" style="font-weight:700; font-size:18px;">{{ losses }}</span></div>
            <div><span style="color:#666; font-size:12px;">BREAKEVENS</span><br><span class="neu" style="font-weight:700; font-size:18px;">{{ bes }}</span></div>
        </div>
    </div>
  </div>
  
  <div class="card" style="margin-top:20px;">
    <h2>Execution Log</h2>
    <table>
      <tr><th>Ticket</th><th>Side</th><th>Entry</th><th>Lot</th><th>Status</th><th>P&L</th><th>Time (UTC)</th></tr>
      {% for t in trades %}
      <tr>
        <td class="neu">#{{ t.ticket if t.ticket else '---' }}</td>
        <td style="font-weight:700" class="{{ 'pos' if t.action=='LONG' else 'neg' }}">{{ t.action }}</td>
        <td>{{ "%.2f"|format(t.entry) }}</td>
        <td>{{ "%.2f"|format(t.lot_size) }}</td>
        <td><span class="badge badge-{{ t.status }}">{{ t.status }}</span></td>
        <td class="{{ 'pos' if t.pnl>0 else 'neg' if t.pnl<0 else 'neu' }}" style="font-weight:800">
            {{ "+$" if t.pnl>0 else "-$" if t.pnl<0 else "$" }}{{ "%.2f"|format(t.pnl|abs) }}
        </td>
        <td class="neu">{{ t.opened_at if t.opened_at else '---' }}</td>
      </tr>
      {% endfor %}
    </table>
  </div>
</body>
</html>
"""

@app.route("/")
def index():
    with get_db() as conn:
        acc = conn.execute("SELECT * FROM account_sync WHERE id=1").fetchone()
        trades = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 20").fetchall()
        
        wins = sum(1 for t in trades if t["status"] == "WIN")
        losses = sum(1 for t in trades if t["status"] == "LOSS")
        bes = sum(1 for t in trades if t["status"] == "BE")
        
    return render_template_string(DASHBOARD, acc=acc, trades=trades, wins=wins, losses=losses, bes=bes)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
