import os
import sqlite3
import threading
import time
import requests
import json
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

lock = threading.Lock()
open_trade = None
last_price = None
current_candle = None  # Tracks the live 5M candle

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
    global open_trade, last_price, current_candle
    if not OANDA_TOKEN or not ACCOUNT_ID:
        print("[Monitor] Variables missing.")
        return

    while True:
        try:
            bid, ask = get_price()
            mid = (bid + ask) / 2.0
            now_ts = int(time.time())
            m5_ts = now_ts - (now_ts % 300) # Round down to nearest 5 min

            with lock:
                last_price = {"bid": bid, "ask": ask, "mid": mid}
                
                # Build live candle for TradingView
                if current_candle is None or current_candle["time"] != m5_ts:
                    current_candle = {"time": m5_ts, "open": mid, "high": mid, "low": mid, "close": mid}
                else:
                    current_candle["high"] = max(current_candle["high"], mid)
                    current_candle["low"]  = min(current_candle["low"], mid)
                    current_candle["close"] = mid

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

@app.route("/api/candles")
def api_candles():
    """Fetches historical 5M candles from OANDA for the chart initialization."""
    try:
        headers = {"Authorization": f"Bearer {OANDA_TOKEN}"}
        params = {"count": 150, "granularity": "M5", "price": "M"}
        url = f"https://api-fxpractice.oanda.com/v3/instruments/{INSTRUMENT}/candles"
        r = requests.get(url, headers=headers, params=params, timeout=10)
        r.raise_for_status()
        candles = []
        for c in r.json().get("candles", []):
            if c["complete"]:
                dt = datetime.strptime(c["time"].split(".")[0] + "Z", "%Y-%m-%dT%H:%M:%SZ")
                candles.append({
                    "time": int(dt.replace(tzinfo=timezone.utc).timestamp()),
                    "open": float(c["mid"]["o"]),
                    "high": float(c["mid"]["h"]),
                    "low": float(c["mid"]["l"]),
                    "close": float(c["mid"]["c"])
                })
        return jsonify(candles)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
<title>Apex Paper Bot | Live Terminal</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<script src="https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js"></script>
<style>
  :root {
    --bg-dark: #0a0c14; --bg-card: rgba(22, 27, 46, 0.65); --border-color: rgba(255, 255, 255, 0.06);
    --text-main: #e2e8f0; --text-muted: #94a3b8;
    --color-win: #10b981; --color-loss: #f43f5e;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; font-family: 'Inter', sans-serif; }
  body { background: #0a0c14; color: var(--text-main); font-size: 14px; -webkit-font-smoothing: antialiased; }
  .container { max-width: 1400px; margin: 0 auto; padding: 20px; }
  
  .header { display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 20px; flex-wrap: wrap; gap: 16px; }
  .title-group h1 { font-size: 26px; font-weight: 800; background: linear-gradient(to right, #fff, #94a3b8); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
  
  .grid { display: grid; grid-template-columns: 1fr 320px; gap: 20px; margin-bottom: 20px; }
  @media(max-width: 900px) { .grid { grid-template-columns: 1fr; } }
  
  .card { background: var(--bg-card); border: 1px solid var(--border-color); border-radius: 12px; padding: 20px; }
  .card h2 { font-size: 12px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; font-weight: 700; }
  
  /* Chart Container */
  #tvchart { width: 100%; height: 400px; border-radius: 8px; overflow: hidden; }
  
  /* Danger Bar */
  .danger-bar-wrapper { width: 100%; height: 12px; background: rgba(0,0,0,0.4); border-radius: 6px; margin: 16px 0; position: relative; overflow: hidden; border: 1px solid var(--border-color); }
  .danger-bar-fill { height: 100%; width: 50%; background: linear-gradient(90deg, var(--color-loss), var(--color-win)); transition: width 0.3s ease; position: absolute; left: 0; }
  .danger-marker { width: 4px; height: 16px; background: #fff; box-shadow: 0 0 8px #fff; position: absolute; top: -2px; transition: left 0.3s ease; border-radius: 2px; transform: translateX(-50%); z-index: 2; }
  
  /* Price Flashing */
  .price-text { font-size: 38px; font-weight: 800; letter-spacing: -1px; transition: color 0.2s ease; }
  .flash-up { color: var(--color-win) !important; text-shadow: 0 0 15px rgba(16, 185, 129, 0.4); }
  .flash-down { color: var(--color-loss) !important; text-shadow: 0 0 15px rgba(244, 63, 94, 0.4); }
  
  .stat-row { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid rgba(255,255,255,0.04); }
  .stat-label { color: var(--text-muted); font-size: 13px; }
  .stat-val { font-weight: 600; }
  .pos { color: var(--color-win); } .neg { color: var(--color-loss); }
  
  .btn-close { width: 100%; background: rgba(244,63,94,0.15); color: var(--color-loss); border: 1px solid rgba(244,63,94,0.3); padding: 10px; border-radius: 8px; font-weight: 700; cursor: pointer; transition: all 0.2s; margin-top: 10px; }
  .btn-close:hover { background: var(--color-loss); color: #fff; }
</style>
</head>
<body>

<div class="container">
  <div class="header">
    <div class="title-group">
      <h1>Apex Live Terminal</h1>
    </div>
  </div>

  <div class="grid">
    <div class="card" style="padding: 10px;">
      <div id="tvchart"></div>
    </div>

    <div class="card">
      <h2>Live Market & Position</h2>
      
      <div id="price-container" class="price-text" style="color: #fff; margin-bottom: 16px;">
        --.--
      </div>

      <div id="trade-ui">
        <div style="text-align:center; padding: 20px; color: var(--text-muted);">
          ⏳ Waiting for next TradingView signal...
        </div>
      </div>
    </div>
  </div>
</div>

<script>
  // 1. Initialize TradingView Chart
  const chart = LightweightCharts.createChart(document.getElementById('tvchart'), {
      width: document.getElementById('tvchart').clientWidth,
      height: 400,
      layout: { background: { type: 'solid', color: 'transparent' }, textColor: '#94a3b8' },
      grid: { vertLines: { color: 'rgba(255,255,255,0.04)' }, horzLines: { color: 'rgba(255,255,255,0.04)' } },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      rightPriceScale: { borderColor: 'rgba(255,255,255,0.1)' },
      timeScale: { borderColor: 'rgba(255,255,255,0.1)', timeVisible: true }
  });

  const candleSeries = chart.addCandlestickSeries({
      upColor: '#10b981', downColor: '#f43f5e', borderVisible: false,
      wickUpColor: '#10b981', wickDownColor: '#f43f5e'
  });

  // Load Historical Data
  fetch('/api/candles').then(r => r.json()).then(data => {
      if(!data.error) candleSeries.setData(data);
  });

  // Resize chart on window resize
  window.addEventListener('resize', () => {
      chart.applyOptions({ width: document.getElementById('tvchart').clientWidth });
  });

  let prevPrice = 0;
  const lotSize = {{ lot_size }}; // Passed from Flask

  function updateDashboard() {
      fetch('/status').then(r => r.json()).then(data => {
          
          // Update Chart Live Candle
          if(data.current_candle && !data.current_candle.error) {
              candleSeries.update(data.current_candle);
          }

          // Update Price with Flashing Effect
          if(data.last_price) {
              const currentPrice = data.last_price.mid;
              const priceEl = document.getElementById('price-container');
              priceEl.innerText = currentPrice.toFixed(2);
              
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

              // Update Active Trade UI & Danger Bar
              const ui = document.getElementById('trade-ui');
              if(data.open_trade) {
                  const t = data.open_trade;
                  const isLong = t.action === 'LONG';
                  
                  // Calculate Unrealized PnL
                  const pnl = isLong ? (currentPrice - t.entry) * (lotSize*100) : (t.entry - currentPrice) * (lotSize*100);
                  const pnlClass = pnl >= 0 ? 'pos' : 'neg';
                  const sign = pnl >= 0 ? '+' : '';

                  // Calculate Danger Bar Marker Position
                  const range = Math.abs(t.tp1 - t.sl);
                  const currentDist = Math.abs(currentPrice - t.sl);
                  let percent = (currentDist / range) * 100;
                  percent = Math.max(0, Math.min(100, percent)); // Cap at 0-100%

                  ui.innerHTML = `
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 12px;">
                      <span style="font-weight:800; font-size:18px;" class="${isLong ? 'pos' : 'neg'}">${t.action}</span>
                      <span style="font-size:12px; border:1px solid rgba(255,255,255,0.2); padding:2px 8px; border-radius:4px;">RR ${t.rr}</span>
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
                    <div class="stat-row"><span class="stat-label">Unrealized P&L</span><span class="stat-val ${pnlClass}" style="font-size:16px;">${sign}$${pnl.toFixed(2)}</span></div>
                    
                    <button class="btn-close" onclick="closeTrade()">Emergency Close</button>
                  `;
              } else {
                  ui.innerHTML = `<div style="text-align:center; padding: 30px 10px; color: var(--text-muted);">
                    <div style="font-size:24px; margin-bottom:10px; opacity:0.5;">⚖️</div>
                    Flat Market<br><span style="font-size:12px; opacity:0.6;">Waiting for next TradingView signal</span>
                  </div>`;
              }
          }
      });
  }

  function closeTrade() {
      if(confirm('Close position at current market price?')) {
          fetch('/close', {method: 'POST'}).then(r => r.json()).then(d => {
              updateDashboard(); // instantly refresh UI
          });
      }
  }

  // Poll server every 2 seconds
  setInterval(updateDashboard, 2000);
  updateDashboard(); // Initial run
</script>

</body>
</html>
"""

@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD, lot_size=LOT_SIZE)

@app.route("/status")
def status():
    with lock:
        return jsonify({
            "last_price": last_price,
            "current_candle": current_candle,
            "open_trade": open_trade
        })

init_db()
monitor_thread = threading.Thread(target=price_monitor, daemon=True)
monitor_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
