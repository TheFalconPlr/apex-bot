import os
import sqlite3
import threading
import time
import requests
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

OANDA_TOKEN = os.environ.get("OANDA_TOKEN", "")
ACCOUNT_ID  = os.environ.get("ACCOUNT_ID",  "")
INSTRUMENT  = "XAU_USD"
LOT_SIZE    = float(os.environ.get("LOT_SIZE",  "0.5"))
SLIPPAGE    = float(os.environ.get("SLIPPAGE",  "1.2"))
MIN_RR      = float(os.environ.get("MIN_RR",    "1.0"))
OZ_FULL     = LOT_SIZE * 100
DB_PATH     = "trades.db"
DAILY_GOAL  = 500.0  # Twój dzienny cel zysku w dolarach

lock = threading.Lock()
open_trade = None
last_price = None

# Zmienne dla Centrum Dowodzenia (Market Pulse)
recent_ticks = []
session_high = -float('inf')
session_low = float('inf')

def get_price():
    headers = {"Authorization": f"Bearer {OANDA_TOKEN}"}
    params  = {"instruments": INSTRUMENT}
    r = requests.get(
        f"https://api-fxpractice.oanda.com/v3/accounts/{ACCOUNT_ID}/pricing",
        headers=headers, params=params, timeout=10
    )
    r.raise_for_status()
    data  = r.json()["prices"][0]
    bid   = float(data["bids"][0]["price"])
    ask   = float(data["asks"][0]["price"])
    return bid, ask

def adjust_levels(action, tp1, sl):
    if action == "LONG": return tp1 - SLIPPAGE, sl - SLIPPAGE
    return tp1 + SLIPPAGE, sl + SLIPPAGE

def calc_rr(action, entry, adj_tp1, adj_sl):
    if action == "LONG":
        reward = adj_tp1 - entry; risk = entry - adj_sl
    else:
        reward = entry - adj_tp1; risk = adj_sl - entry
    return (reward / risk) if risk > 0 else 0.0

def calc_pnl(action, entry, exit_price, oz):
    if action == "LONG": return (exit_price - entry) * oz
    return (entry - exit_price) * oz

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, entry REAL NOT NULL,
                sl_raw REAL NOT NULL, tp1_raw REAL NOT NULL, sl REAL NOT NULL, tp1 REAL NOT NULL,
                rr REAL DEFAULT 0, score TEXT DEFAULT '', status TEXT DEFAULT 'OPEN',
                pnl REAL DEFAULT 0, opened_at TEXT, closed_at TEXT
            )
        """)
        conn.commit()

def price_monitor():
    global open_trade, last_price, recent_ticks, session_high, session_low
    if not OANDA_TOKEN or not ACCOUNT_ID:
        print("[Monitor] OANDA credentials missing.")
        return

    while True:
        try:
            bid, ask = get_price()
            mid = (bid + ask) / 2.0
            now_time_str = datetime.now(timezone.utc).strftime("%H:%M:%S")

            with lock:
                # Rejestruj kierunek dla Tick Tape
                direction = "FLAT"
                if last_price:
                    if mid > last_price["mid"]: direction = "UP"
                    elif mid < last_price["mid"]: direction = "DOWN"
                
                last_price = {"bid": bid, "ask": ask, "mid": mid, "spread": round(ask - bid, 2)}
                
                # Aktualizacja statystyk sesji
                session_high = max(session_high, mid)
                session_low = min(session_low, mid)

                # Dodaj do historii ticków
                if direction != "FLAT":
                    recent_ticks.insert(0, {"time": now_time_str, "price": mid, "direction": direction})
                    if len(recent_ticks) > 8:  # Trzymamy tylko 8 ostatnich zmian
                        recent_ticks.pop()

                # Sprawdzanie otwartej pozycji
                if open_trade is not None:
                    t = open_trade
                    action = t["action"]
                    tp1_hit = ask >= t["tp1"] if action == "LONG" else bid <= t["tp1"]
                    sl_hit  = bid <= t["sl"]  if action == "LONG" else ask >= t["sl"]
                    now_str = datetime.now(timezone.utc).isoformat()

                    if tp1_hit or sl_hit:
                        exit_p = t["tp1"] if tp1_hit else t["sl"]
                        status = 'WIN' if tp1_hit else 'LOSS'
                        pnl = round(calc_pnl(action, t["entry"], exit_p, OZ_FULL), 2)
                        with get_db() as conn:
                            conn.execute("UPDATE trades SET status=?, pnl=?, closed_at=? WHERE id=?", (status, pnl, now_str, t["id"]))
                            conn.commit()
                        open_trade = None

            time.sleep(2)
        except Exception as e:
            time.sleep(10)

@app.route("/webhook", methods=["POST"])
def webhook():
    global open_trade
    try:
        data = request.get_json(force=True)
        action = str(data.get("action", "")).upper()
        if action not in ("LONG", "SHORT"): return jsonify({"error": "invalid action"}), 400
        entry, sl_raw, tp1_raw = float(data["entry"]), float(data["sl"]), float(data["tp1"])
        adj_tp1, adj_sl = adjust_levels(action, tp1_raw, sl_raw)
        rr = calc_rr(action, entry, adj_tp1, adj_sl)
        
        if rr < MIN_RR: return jsonify({"status": "skipped", "reason": "RR too low"}), 200
        
        with lock:
            if open_trade is not None: return jsonify({"status": "skipped", "reason": "already in trade"}), 200
            now_str = datetime.now(timezone.utc).isoformat()
            with get_db() as conn:
                cur = conn.execute(
                    "INSERT INTO trades (action,entry,sl_raw,tp1_raw,sl,tp1,rr,status,opened_at) VALUES (?,?,?,?,?,?,?,'OPEN',?)",
                    (action, entry, sl_raw, tp1_raw, adj_sl, adj_tp1, round(rr, 2), now_str))
                trade_id = cur.lastrowid
                conn.commit()
            open_trade = {"id": trade_id, "action": action, "entry": entry, "sl": adj_sl, "tp1": adj_tp1, "rr": round(rr, 2)}
        return jsonify({"status": "ok", "trade_id": trade_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/close", methods=["POST"])
def manual_close():
    global open_trade
    with lock:
        if open_trade is None: return jsonify({"status": "no open trade"}), 200
        price = last_price["mid"] if last_price else open_trade["entry"]
        pnl = round(calc_pnl(open_trade["action"], open_trade["entry"], price, OZ_FULL), 2)
        with get_db() as conn:
            conn.execute("UPDATE trades SET status='MANUAL', pnl=?, closed_at=? WHERE id=?", 
                         (pnl, datetime.now(timezone.utc).isoformat(), open_trade["id"]))
            conn.commit()
        open_trade = None
    return jsonify({"status": "closed"})

# ----------------- HTML & JS DASHBOARD -----------------
DASHBOARD = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Apex Paper Bot | Command Center</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  :root {
    --bg-dark: #0a0c14; --bg-card: rgba(22, 27, 46, 0.65); --border-color: rgba(255, 255, 255, 0.06);
    --text-main: #e2e8f0; --text-muted: #94a3b8;
    --color-win: #10b981; --color-win-bg: rgba(16, 185, 129, 0.15);
    --color-loss: #f43f5e; --color-loss-bg: rgba(244, 63, 94, 0.15);
    --color-open: #3b82f6; --color-open-bg: rgba(59, 130, 246, 0.15);
    --color-manual: #8b5cf6; --color-manual-bg: rgba(139, 92, 246, 0.15);
  }
  * { margin: 0; padding: 0; box-sizing: border-box; font-family: 'Inter', sans-serif; }
  body { background: radial-gradient(circle at top right, #151a30, var(--bg-dark)); color: var(--text-main); font-size: 14px; min-height: 100vh; padding-bottom: 50px; }
  .container { max-width: 1400px; margin: 0 auto; padding: 20px; }
  
  /* Header */
  .header { display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 20px; flex-wrap: wrap; gap: 16px; }
  .title-group h1 { font-size: 26px; font-weight: 800; background: linear-gradient(to right, #fff, #94a3b8); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
  .title-group p { color: var(--text-muted); font-size: 13px; font-weight: 500; margin-top: 4px; }
  .tags { display: flex; gap: 8px; flex-wrap: wrap; }
  .tag { background: rgba(255,255,255,0.03); border: 1px solid var(--border-color); padding: 4px 10px; border-radius: 20px; font-size: 11px; font-weight: 600; color: var(--text-muted); }
  
  /* Cards */
  .card { background: var(--bg-card); backdrop-filter: blur(12px); border: 1px solid var(--border-color); border-radius: 12px; padding: 20px; box-shadow: 0 10px 30px -10px rgba(0,0,0,0.5); }
  .card h2 { font-size: 12px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 16px; font-weight: 700; }
  
  /* Top Grid */
  .top-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; margin-bottom: 20px; }
  .pnl-big { font-size: 36px; font-weight: 800; margin-bottom: 16px; letter-spacing: -1px; }
  
  /* UI Elements */
  .stat-row, .level-row { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid rgba(255,255,255,0.04); }
  .stat-row:last-child { border-bottom: none; }
  .stat-label { color: var(--text-muted); font-weight: 500; }
  .stat-val { font-weight: 700; }
  .pos { color: var(--color-win); } .neg { color: var(--color-loss); } .neu { color: var(--text-muted); }
  
  .danger-bar-wrapper { width: 100%; height: 10px; background: rgba(0,0,0,0.4); border-radius: 5px; margin: 16px 0; position: relative; overflow: hidden; border: 1px solid var(--border-color); }
  .danger-bar-fill { height: 100%; width: 50%; background: linear-gradient(90deg, var(--color-loss), var(--color-win)); transition: width 0.3s ease; position: absolute; left: 0; }
  .danger-marker { width: 4px; height: 14px; background: #fff; box-shadow: 0 0 8px #fff; position: absolute; top: -2px; transition: left 0.3s ease; border-radius: 2px; transform: translateX(-50%); z-index: 2; }
  
  .price-text { font-size: 42px; font-weight: 800; letter-spacing: -1px; transition: color 0.2s ease; margin-bottom: 16px; color: #fff;}
  .flash-up { color: var(--color-win) !important; text-shadow: 0 0 15px rgba(16, 185, 129, 0.4); }
  .flash-down { color: var(--color-loss) !important; text-shadow: 0 0 15px rgba(244, 63, 94, 0.4); }
  
  .btn-close { width: 100%; background: var(--color-loss-bg); color: var(--color-loss); border: 1px solid rgba(244,63,94,0.3); padding: 12px; border-radius: 8px; font-weight: 700; cursor: pointer; transition: all 0.2s; margin-top: 16px; }
  .btn-close:hover { background: var(--color-loss); color: #fff; }

  /* Command Center Grid */
  .command-grid { display: grid; grid-template-columns: 2fr 1fr; gap: 20px; margin-bottom: 20px; }
  @media(max-width: 900px) { .command-grid { grid-template-columns: 1fr; } }
  
  /* Tick Tape */
  .tick-tape-container { display: flex; flex-direction: column; gap: 10px; min-height: 220px; }
  .tick-item { display: flex; justify-content: space-between; align-items: center; background: rgba(0,0,0,0.2); padding: 12px 16px; border-radius: 8px; border-left: 4px solid transparent; animation: slideIn 0.3s ease-out; font-size: 15px; }
  @keyframes slideIn { from { opacity: 0; transform: translateY(-10px); } to { opacity: 1; transform: translateY(0); } }
  .tick-up { border-left-color: var(--color-win); }
  .tick-down { border-left-color: var(--color-loss); }

  /* Daily Target */
  .target-wrap { margin-top: 15px; }
  .target-bg { width: 100%; background: rgba(0,0,0,0.4); height: 16px; border-radius: 8px; overflow: hidden; border: 1px solid var(--border-color); }
  .target-fill { height: 100%; background: linear-gradient(90deg, var(--color-open), var(--color-win)); transition: width 0.5s ease; }

  /* Table */
  .table-container { background: var(--bg-card); backdrop-filter: blur(12px); border: 1px solid var(--border-color); border-radius: 16px; overflow-x: auto; margin-top: 20px;}
  .table-header { padding: 16px 20px; border-bottom: 1px solid var(--border-color); font-size: 14px; font-weight: 700; }
  table { width: 100%; border-collapse: collapse; text-align: left; }
  th { font-size: 11px; color: var(--text-muted); text-transform: uppercase; padding: 12px 20px; background: rgba(0,0,0,0.2); font-weight: 700; }
  td { padding: 12px 20px; border-bottom: 1px solid rgba(255,255,255,0.03); font-weight: 500; font-size: 13px;}
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(255,255,255,0.02); }
  .badge { padding: 4px 8px; border-radius: 4px; font-size: 10px; font-weight: 700; text-transform: uppercase; }
  .badge-WIN { background: var(--color-win-bg); color: var(--color-win); }
  .badge-LOSS { background: var(--color-loss-bg); color: var(--color-loss); }
  .badge-OPEN { background: var(--color-open-bg); color: var(--color-open); }
  .badge-MANUAL { background: var(--color-manual-bg); color: var(--color-manual); }
</style>
</head>
<body>

<div class="container">
  
  <div class="header">
    <div class="title-group">
      <h1>Apex Live Terminal</h1>
      <p>{{ month }} — Real-Time Command Center</p>
    </div>
    <div class="tags">
      <span class="tag">Zero External Scripts</span>
      <span class="tag">Native Market Pulse</span>
      <span class="tag">{{ lot_size }} Lot</span>
    </div>
  </div>

  <div class="top-grid">
    <div class="card">
      <h2>{{ month }} Performance</h2>
      {% if total >= 0 %}
        <div class="pnl-big pos">+${{ "%.0f"|format(total) }}</div>
      {% else %}
        <div class="pnl-big neg">-${{ "%.0f"|format(total|abs) }}</div>
      {% endif %}
      <div class="stat-row"><span class="stat-label">Total Trades</span><span class="stat-val">{{ count }}</span></div>
      <div class="stat-row"><span class="stat-label">Win Rate</span><span class="stat-val {% if win_rate >= 50 %}pos{% else %}neu{% endif %}">{{ win_rate }}%</span></div>
      <div class="stat-row"><span class="stat-label">Wins / Losses</span><span class="stat-val"><span class="pos">{{ wins }}</span> / <span class="neg">{{ losses }}</span></span></div>
      <div class="stat-row"><span class="stat-label">Best Trade</span><span class="stat-val pos">{% if best > 0 %}+${{ "%.0f"|format(best) }}{% else %}—{% endif %}</span></div>
    </div>

    <div class="card">
      <h2>Active Position</h2>
      <div id="trade-ui">
        <div style="display:flex; flex-direction:column; align-items:center; justify-content:center; height:150px; text-align:center;">
          <div style="font-size:24px; margin-bottom:8px; opacity:0.5;">⏳</div>
          <div style="font-size:16px; font-weight:700; color:var(--text-muted);">Fetching Data...</div>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>XAU/USD Live Price</h2>
      <div id="connection-status" style="font-size:12px; font-weight:600; color:var(--text-muted); margin-bottom:12px;">
        Connecting to API...
      </div>
      <div id="price-container" class="price-text">--.--</div>
      <div class="stat-row"><span class="stat-label">Ask (Buy)</span><span class="stat-val" id="price-ask">--.--</span></div>
      <div class="stat-row"><span class="stat-label">Bid (Sell)</span><span class="stat-val" id="price-bid">--.--</span></div>
    </div>
  </div>

  <div class="command-grid">
    <div class="card">
      <h2>Live Tick Tape</h2>
      <div class="tick-tape-container" id="tick-tape">
        <div style="color:var(--text-muted); font-size:13px; padding:20px 0;">Waiting for price movements...</div>
      </div>
    </div>
    
    <div class="card" style="display:flex; flex-direction:column; justify-content:space-between;">
      <div>
        <h2>Market Pulse</h2>
        <div class="stat-row"><span class="stat-label">Live Spread</span><span class="stat-val" id="live-spread" style="color:var(--color-open);">-- pts</span></div>
        <div class="stat-row"><span class="stat-label">Session High</span><span class="stat-val" id="session-high">--.--</span></div>
        <div class="stat-row"><span class="stat-label">Session Low</span><span class="stat-val" id="session-low">--.--</span></div>
      </div>
      
      <div class="target-wrap">
        <div style="display:flex; justify-content:space-between; margin-bottom:8px;">
          <h2 style="margin:0;">Daily Target</h2>
          <span style="font-size:12px; font-weight:700;" class="{% if daily_pnl >= 0 %}pos{% else %}neg{% endif %}">
            ${{ "%.0f"|format(daily_pnl) }} / ${{ "%.0f"|format(daily_goal) }}
          </span>
        </div>
        <div class="target-bg">
          <div class="target-fill" style="width: {{ target_percent }}%;"></div>
        </div>
      </div>
    </div>
  </div>

  <div class="table-container">
    <div class="table-header">Trade History</div>
    {% if trades %}
    <table>
      <thead>
        <tr>
          <th>ID</th><th>Side</th><th>Entry</th><th>SL</th><th>TP1</th><th>RR</th><th>Status</th><th>P&L</th><th>Date (UTC)</th>
        </tr>
      </thead>
      <tbody>
      {% for t in trades %}
        <tr>
          <td style="color:var(--text-muted);">#{{ t.id }}</td>
          <td class="{% if t.action=='LONG' %}pos{% else %}neg{% endif %}" style="font-weight:700;">{{ t.action }}</td>
          <td>{{ "%.2f"|format(t.entry) }}</td>
          <td style="color:var(--color-loss);">{{ "%.2f"|format(t.sl) }}</td>
          <td style="color:var(--color-win);">{{ "%.2f"|format(t.tp1) }}</td>
          <td style="color:var(--text-muted);">{{ "%.2f"|format(t.rr) }}</td>
          <td><span class="badge badge-{{ t.status }}">{{ t.status }}</span></td>
          <td class="{% if t.pnl>0 %}pos{% elif t.pnl<0 %}neg{% else %}neu{% endif %}" style="font-weight:800;">
            {% if t.status=='OPEN' %}—{% elif t.pnl>=0 %}+${{ "%.0f"|format(t.pnl) }}{% else %}-${{ "%.0f"|format(t.pnl|abs) }}{% endif %}
          </td>
          <td style="color:var(--text-muted);">{{ t.opened_at[:16].replace('T', ' ') if t.opened_at else '—' }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    {% else %}
      <div style="padding: 30px; text-align: center; color: var(--text-muted);">No trades registered yet.</div>
    {% endif %}
  </div>

</div>

<script>
  let prevPrice = 0;
  let activeTradeId = null;
  const lotSize = {{ lot_size }}; 

  function updateDashboard() {
      fetch('/status').then(r => r.json()).then(data => {
          
          if(data.last_price && data.last_price.mid) {
              
              document.getElementById('connection-status').innerHTML = '<span style="color:var(--color-win);">● API Connected</span>';

              // Aktualizacja Cen
              const currentPrice = data.last_price.mid;
              const priceEl = document.getElementById('price-container');
              priceEl.innerText = currentPrice.toFixed(2);
              document.getElementById('price-ask').innerText = data.last_price.ask.toFixed(2);
              document.getElementById('price-bid').innerText = data.last_price.bid.toFixed(2);
              
              if(currentPrice > prevPrice && prevPrice !== 0) {
                  priceEl.classList.remove('flash-down');
                  priceEl.classList.add('flash-up');
                  setTimeout(() => priceEl.classList.remove('flash-up'), 500);
              } else if (currentPrice < prevPrice && prevPrice !== 0) {
                  priceEl.classList.remove('flash-up');
                  priceEl.classList.add('flash-down');
                  setTimeout(() => priceEl.classList.remove('flash-down'), 500);
              }
              prevPrice = currentPrice;

              // Market Pulse (Spread, High/Low)
              document.getElementById('live-spread').innerText = data.last_price.spread.toFixed(2) + ' pts';
              if (data.session_high !== null) document.getElementById('session-high').innerText = data.session_high.toFixed(2);
              if (data.session_low !== null) document.getElementById('session-low').innerText = data.session_low.toFixed(2);

              // Tick Tape
              if(data.recent_ticks && data.recent_ticks.length > 0) {
                  const tape = document.getElementById('tick-tape');
                  tape.innerHTML = ''; // Wyczyść stare
                  data.recent_ticks.forEach(tick => {
                      const row = document.createElement('div');
                      row.className = `tick-item ${tick.direction === 'UP' ? 'tick-up' : (tick.direction === 'DOWN' ? 'tick-down' : '')}`;
                      
                      const icon = tick.direction === 'UP' ? '▲' : '▼';
                      const colorClass = tick.direction === 'UP' ? 'pos' : 'neg';
                      
                      row.innerHTML = `
                        <span style="color:var(--text-muted); font-size:12px;">${tick.time} UTC</span> 
                        <span class="${colorClass}" style="font-weight:700; letter-spacing:1px;">${icon} ${tick.price.toFixed(2)}</span>
                      `;
                      tape.appendChild(row);
                  });
              }

              // Panel Otwarta Pozycja
              const ui = document.getElementById('trade-ui');
              if(data.open_trade) {
                  const t = data.open_trade;
                  const isLong = t.action === 'LONG';
                  
                  if (activeTradeId !== t.id && activeTradeId !== null) window.location.reload(); 
                  activeTradeId = t.id;

                  const pnl = isLong ? (currentPrice - t.entry) * (lotSize*100) : (t.entry - currentPrice) * (lotSize*100);
                  const pnlClass = pnl >= 0 ? 'pos' : 'neg';
                  const sign = pnl >= 0 ? '+' : '';

                  const range = Math.abs(t.tp1 - t.sl);
                  const currentDist = Math.abs(currentPrice - t.sl);
                  let percent = (currentDist / range) * 100;
                  percent = Math.max(0, Math.min(100, percent));

                  ui.innerHTML = `
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 12px;">
                      <span style="font-weight:800; font-size:22px;" class="${isLong ? 'pos' : 'neg'}">${t.action}</span>
                      <span class="badge badge-OPEN">RR ${t.rr}</span>
                    </div>
                    
                    <div style="display:flex; justify-content:space-between; font-size:11px; color:#94a3b8;">
                      <span>SL: ${t.sl.toFixed(2)}</span>
                      <span>TP: ${t.tp1.toFixed(2)}</span>
                    </div>
                    <div class="danger-bar-wrapper">
                      <div class="danger-bar-fill"></div>
                      <div class="danger-marker" style="left: ${percent}%;"></div>
                    </div>

                    <div class="stat-row"><span class="stat-label">Entry</span><span class="stat-val">${t.entry.toFixed(2)}</span></div>
                    <div class="stat-row"><span class="stat-label">Unrealized P&L</span><span class="stat-val ${pnlClass}" style="font-size:18px;">${sign}$${pnl.toFixed(2)}</span></div>
                    
                    <button class="btn-close" onclick="closeTrade()">Emergency Close</button>
                  `;
              } else {
                  if (activeTradeId !== null) {
                      setTimeout(() => window.location.reload(), 1500);
                      activeTradeId = null;
                  }
                  ui.innerHTML = `
                    <div style="display:flex; flex-direction:column; align-items:center; justify-content:center; height:150px; text-align:center;">
                      <div style="font-size:28px; margin-bottom:8px; opacity:0.5;">⚖️</div>
                      <div style="font-size:16px; font-weight:700; color:var(--text-muted);">Flat Market</div>
                    </div>
                  `;
              }
          }
      });
  }

  function closeTrade() {
      if(confirm('Warning: Close position at current market price?')) {
          fetch('/close', {method: 'POST'}).then(r => r.json()).then(d => {
              window.location.reload();
          });
      }
  }

  setInterval(updateDashboard, 2000);
  updateDashboard();
</script>

</body>
</html>
"""

@app.route("/")
def dashboard():
    now = datetime.now(timezone.utc)
    month_start = f"{now.year}-{now.month:02d}-01"
    today_start = f"{now.year}-{now.month:02d}-{now.day:02d}"
    
    with get_db() as conn:
        month_rows = conn.execute(
            "SELECT * FROM trades WHERE status != 'OPEN' AND opened_at >= ? ORDER BY opened_at DESC",
            (month_start,)).fetchall()
        all_rows = conn.execute(
            "SELECT * FROM trades ORDER BY opened_at DESC LIMIT 50").fetchall()
            
    closed  = [dict(r) for r in month_rows]
    all_t   = [dict(r) for r in all_rows]
    
    total   = sum(t["pnl"] for t in closed)
    wins    = sum(1 for t in closed if t["status"] == "WIN")
    losses  = sum(1 for t in closed if t["status"] == "LOSS")
    count   = len(closed)
    wr      = round(wins / count * 100) if count > 0 else 0
    best    = max((t["pnl"] for t in closed), default=0)
    
    # Obliczanie dziennego postępu (Daily PnL)
    today_rows = [t for t in closed if t.get("closed_at") and t["closed_at"] >= today_start]
    daily_pnl  = sum(t["pnl"] for t in today_rows)
    target_percent = min(100, max(0, (daily_pnl / DAILY_GOAL) * 100)) if DAILY_GOAL > 0 else 0
    
    return render_template_string(DASHBOARD,
        month=now.strftime("%b %Y"), total=total, wins=wins, losses=losses,
        count=count, win_rate=wr, best=best, trades=all_t, 
        lot_size=LOT_SIZE, slippage=SLIPPAGE, min_rr=MIN_RR,
        daily_pnl=daily_pnl, daily_goal=DAILY_GOAL, target_percent=target_percent)

@app.route("/status")
def status():
    with lock:
        # Zabezpieczenie przed błędem, gdy API OANDA jeszcze nie zwróciło danych sesji
        safe_high = session_high if session_high != -float('inf') else None
        safe_low = session_low if session_low != float('inf') else None
        
        return jsonify({
            "last_price": last_price,
            "recent_ticks": recent_ticks,
            "session_high": safe_high,
            "session_low": safe_low,
            "open_trade": open_trade
        })

init_db()
monitor_thread = threading.Thread(target=price_monitor, daemon=True)
monitor_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
