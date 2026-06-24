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

OANDA_URL   = f"https://api-fxpractice.oanda.com/v3/accounts/{ACCOUNT_ID}/pricing"

lock       = threading.Lock()
open_trade = None
last_price = None

def get_price():
    """Fetch current XAU/USD bid/ask from OANDA practice REST API."""
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
    if action == "LONG":
        return tp1 - SLIPPAGE, sl - SLIPPAGE
    return tp1 + SLIPPAGE, sl + SLIPPAGE

def calc_rr(action, entry, adj_tp1, adj_sl):
    if action == "LONG":
        reward = adj_tp1 - entry
        risk   = entry   - adj_sl
    else:
        reward = entry   - adj_tp1
        risk   = adj_sl  - entry
    return (reward / risk) if risk > 0 else 0.0

def calc_pnl(action, entry, exit_price, oz):
    if action == "LONG":
        return (exit_price - entry) * oz
    return (entry - exit_price) * oz

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                action     TEXT    NOT NULL,
                entry      REAL    NOT NULL,
                sl_raw     REAL    NOT NULL,
                tp1_raw    REAL    NOT NULL,
                sl         REAL    NOT NULL,
                tp1        REAL    NOT NULL,
                rr         REAL    DEFAULT 0,
                score      TEXT    DEFAULT '',
                status     TEXT    DEFAULT 'OPEN',
                pnl        REAL    DEFAULT 0,
                opened_at  TEXT,
                closed_at  TEXT
            )
        """)
        conn.commit()

def price_monitor():
    global open_trade, last_price

    if not OANDA_TOKEN or not ACCOUNT_ID:
        print("[Monitor] OANDA_TOKEN or ACCOUNT_ID not set — check Railway Variables.")
        return

    print(f"[Monitor] Starting with account {ACCOUNT_ID}")

    while True:
        try:
            bid, ask = get_price()
            mid = (bid + ask) / 2.0

            with lock:
                last_price = {"bid": bid, "ask": ask, "mid": mid}

                if open_trade is not None:
                    t      = open_trade
                    action = t["action"]

                    if action == "LONG":
                        tp1_hit = ask >= t["tp1"]
                        sl_hit  = bid <= t["sl"]
                    else:
                        tp1_hit = bid <= t["tp1"]
                        sl_hit  = ask >= t["sl"]

                    now_str = datetime.now(timezone.utc).isoformat()

                    if tp1_hit:
                        pnl = round(calc_pnl(action, t["entry"], t["tp1"], OZ_FULL), 2)
                        with get_db() as conn:
                            conn.execute(
                                "UPDATE trades SET status='WIN', pnl=?, closed_at=? WHERE id=?",
                                (pnl, now_str, t["id"]))
                            conn.commit()
                        print(f"[Trade #{t['id']}] WIN  +${pnl:.2f}")
                        open_trade = None

                    elif sl_hit:
                        pnl = round(calc_pnl(action, t["entry"], t["sl"], OZ_FULL), 2)
                        with get_db() as conn:
                            conn.execute(
                                "UPDATE trades SET status='LOSS', pnl=?, closed_at=? WHERE id=?",
                                (pnl, now_str, t["id"]))
                            conn.commit()
                        print(f"[Trade #{t['id']}] LOSS  ${pnl:.2f}")
                        open_trade = None

            time.sleep(2)

        except requests.exceptions.HTTPError as e:
            print(f"[Monitor] OANDA HTTP error: {e.response.status_code} — {e.response.text[:200]}")
            time.sleep(15)
        except Exception as e:
            print(f"[Monitor] Error: {e} — retrying in 10s")
            time.sleep(10)

@app.route("/webhook", methods=["POST"])
def webhook():
    global open_trade
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "no JSON"}), 400
        action = str(data.get("action", "")).upper()
        if action not in ("LONG", "SHORT"):
            return jsonify({"error": f"invalid action: {action}"}), 400
        entry   = float(data["entry"])
        sl_raw  = float(data["sl"])
        tp1_raw = float(data["tp1"])
        score   = str(data.get("score", ""))
        adj_tp1, adj_sl = adjust_levels(action, tp1_raw, sl_raw)
        rr = calc_rr(action, entry, adj_tp1, adj_sl)
        if rr < MIN_RR:
            print(f"[Signal] Skipped — RR {rr:.2f} < {MIN_RR}")
            return jsonify({"status": "skipped", "reason": f"RR {rr:.2f} below minimum {MIN_RR}"}), 200
        with lock:
            if open_trade is not None:
                return jsonify({"status": "skipped", "reason": "already in trade", "open_id": open_trade["id"]}), 200
            now_str = datetime.now(timezone.utc).isoformat()
            with get_db() as conn:
                cur = conn.execute(
                    "INSERT INTO trades (action,entry,sl_raw,tp1_raw,sl,tp1,rr,score,status,opened_at) VALUES (?,?,?,?,?,?,?,?,'OPEN',?)",
                    (action, entry, sl_raw, tp1_raw, adj_sl, adj_tp1, round(rr, 2), score, now_str))
                trade_id = cur.lastrowid
                conn.commit()
            open_trade = {"id": trade_id, "action": action, "entry": entry,
                          "sl": adj_sl, "tp1": adj_tp1, "rr": round(rr, 2)}
        print(f"[Trade #{trade_id}] {action} @ {entry}  SL:{adj_sl}  TP1:{adj_tp1}  RR:{rr:.2f}")
        return jsonify({"status": "ok", "trade_id": trade_id, "rr": round(rr, 2)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/close", methods=["POST"])
def manual_close():
    global open_trade
    with lock:
        if open_trade is None:
            return jsonify({"status": "no open trade"}), 200
        price = last_price["mid"] if last_price else open_trade["entry"]
        pnl   = round(calc_pnl(open_trade["action"], open_trade["entry"], price, OZ_FULL), 2)
        now_str = datetime.now(timezone.utc).isoformat()
        with get_db() as conn:
            conn.execute("UPDATE trades SET status='MANUAL', pnl=?, closed_at=? WHERE id=?",
                (pnl, now_str, open_trade["id"]))
            conn.commit()
        trade_id   = open_trade["id"]
        open_trade = None
    return jsonify({"status": "closed", "trade_id": trade_id, "pnl": pnl})

DASHBOARD = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta http-equiv="refresh" content="10">
<title>Apex Paper Bot | Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  :root {
    --bg-dark: #0a0c14;
    --bg-card: rgba(22, 27, 46, 0.65);
    --border-color: rgba(255, 255, 255, 0.06);
    --text-main: #e2e8f0;
    --text-muted: #94a3b8;
    --color-win: #10b981;
    --color-win-bg: rgba(16, 185, 129, 0.15);
    --color-loss: #f43f5e;
    --color-loss-bg: rgba(244, 63, 94, 0.15);
    --color-open: #3b82f6;
    --color-open-bg: rgba(59, 130, 246, 0.15);
    --color-manual: #8b5cf6;
    --color-manual-bg: rgba(139, 92, 246, 0.15);
  }
  * { margin: 0; padding: 0; box-sizing: border-box; font-family: 'Inter', sans-serif; }
  body { 
    background: radial-gradient(circle at top right, #151a30, var(--bg-dark)); 
    color: var(--text-main); 
    min-height: 100vh; 
    font-size: 14px;
    -webkit-font-smoothing: antialiased;
  }
  
  /* Layout */
  .container { max-width: 1400px; margin: 0 auto; padding: 30px; }
  .header { display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 24px; flex-wrap: wrap; gap: 16px; }
  .title-group h1 { font-size: 28px; font-weight: 800; background: linear-gradient(to right, #ffffff, #94a3b8); -webkit-background-clip: text; -webkit-text-fill-color: transparent; letter-spacing: -0.5px; }
  .title-group p { color: var(--text-muted); font-size: 14px; margin-top: 4px; font-weight: 500; }
  
  .tags { display: flex; gap: 8px; flex-wrap: wrap; }
  .tag { background: rgba(255,255,255,0.03); border: 1px solid var(--border-color); padding: 6px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; color: var(--text-muted); backdrop-filter: blur(4px); }
  
  /* Cards Grid */
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; margin-bottom: 30px; }
  .card { 
    background: var(--bg-card); 
    backdrop-filter: blur(12px); 
    border: 1px solid var(--border-color); 
    border-radius: 16px; 
    padding: 24px; 
    box-shadow: 0 10px 30px -10px rgba(0,0,0,0.5);
    transition: transform 0.2s ease, border-color 0.2s ease;
  }
  .card:hover { border-color: rgba(255,255,255,0.15); transform: translateY(-2px); }
  .card h2 { font-size: 12px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 1.2px; margin-bottom: 16px; font-weight: 700; }
  
  /* Typography & Colors */
  .pnl-big { font-size: 42px; font-weight: 800; margin-bottom: 20px; letter-spacing: -1px; }
  .pos { color: var(--color-win); } 
  .neg { color: var(--color-loss); } 
  .neu { color: var(--text-muted); }
  
  /* Stats & Levels */
  .stat-row, .level-row { display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid rgba(255,255,255,0.04); }
  .stat-row:last-child, .level-row:last-child { border-bottom: none; }
  .stat-label { color: var(--text-muted); font-weight: 500; }
  .stat-val { font-weight: 700; }
  
  /* Buttons */
  .btn-close { 
    width: 100%; background: var(--color-loss-bg); color: var(--color-loss); border: 1px solid rgba(244,63,94,0.3);
    padding: 12px; border-radius: 8px; font-weight: 700; cursor: pointer; transition: all 0.2s ease; margin-top: 16px;
  }
  .btn-close:hover { background: var(--color-loss); color: #fff; box-shadow: 0 0 15px rgba(244,63,94,0.4); }
  
  /* Status Dot */
  .status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; box-shadow: 0 0 8px currentColor; }
  .status-connected { color: var(--color-win); background: var(--color-win); }
  .status-error { color: var(--color-loss); background: var(--color-loss); }

  /* Table */
  .table-container { 
    background: var(--bg-card); backdrop-filter: blur(12px); border: 1px solid var(--border-color); 
    border-radius: 16px; overflow-x: auto; box-shadow: 0 10px 30px -10px rgba(0,0,0,0.5);
  }
  .table-header { padding: 20px 24px; border-bottom: 1px solid var(--border-color); font-size: 14px; font-weight: 700; }
  table { width: 100%; border-collapse: collapse; text-align: left; }
  th { font-size: 11px; color: var(--text-muted); text-transform: uppercase; padding: 14px 24px; background: rgba(0,0,0,0.2); font-weight: 700; letter-spacing: 0.5px; }
  td { padding: 14px 24px; border-bottom: 1px solid rgba(255,255,255,0.03); font-weight: 500; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(255,255,255,0.02); }
  
  /* Badges */
  .badge { padding: 4px 10px; border-radius: 6px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; }
  .badge-WIN { background: var(--color-win-bg); color: var(--color-win); border: 1px solid rgba(16,185,129,0.3); }
  .badge-LOSS { background: var(--color-loss-bg); color: var(--color-loss); border: 1px solid rgba(244,63,94,0.3); }
  .badge-OPEN { background: var(--color-open-bg); color: var(--color-open); border: 1px solid rgba(59,130,246,0.3); }
  .badge-MANUAL { background: var(--color-manual-bg); color: var(--color-manual); border: 1px solid rgba(139,92,246,0.3); }

  /* Custom Scrollbar */
  ::-webkit-scrollbar { width: 8px; height: 8px; }
  ::-webkit-scrollbar-track { background: #0a0c14; }
  ::-webkit-scrollbar-thumb { background: #2a314d; border-radius: 4px; }
  ::-webkit-scrollbar-thumb:hover { background: #3b446b; }
</style>
</head>
<body>

<div class="container">
  
  <div class="header">
    <div class="title-group">
      <h1>Apex Paper Bot</h1>
      <p>{{ month }} — Paper Trading Automated System</p>
    </div>
    <div class="tags">
      <span class="tag">Auto-refreshes 10s</span>
      <span class="tag">100% exit @ TP1</span>
      <span class="tag">{{ slippage }}pt Slippage</span>
      <span class="tag">Min RR {{ min_rr }}:1</span>
      <span class="tag">{{ lot_size }} Lot (${{ (lot_size*100)|int }}/pt)</span>
    </div>
  </div>

  <div class="grid">
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
      <div class="stat-row"><span class="stat-label">Worst Trade</span><span class="stat-val neg">{% if worst < 0 %}-${{ "%.0f"|format(worst|abs) }}{% else %}—{% endif %}</span></div>
    </div>

    <div class="card">
      <h2>Active Position</h2>
      {% if open_trade %}
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px;">
          <div style="font-size:24px; font-weight:800;" class="{% if open_trade.action=='LONG' %}pos{% else %}neg{% endif %}">
            {{ open_trade.action }}
          </div>
          <span class="badge badge-OPEN">RR {{ "%.2f"|format(open_trade.rr) }}</span>
        </div>
        
        <div class="level-row"><span class="stat-label">Entry Price</span><span class="stat-val">{{ "%.2f"|format(open_trade.entry) }}</span></div>
        <div class="level-row"><span class="stat-label">Stop Loss (Adj)</span><span class="stat-val neg">{{ "%.2f"|format(open_trade.sl) }}</span></div>
        <div class="level-row"><span class="stat-label">Take Profit (Adj)</span><span class="stat-val pos">{{ "%.2f"|format(open_trade.tp1) }}</span></div>
        
        <div style="margin-top:16px; padding-top:16px; border-top:1px dashed var(--border-color);">
          <div class="level-row">
            <span class="stat-label">Unrealized P&L</span>
            <span class="stat-val {% if unrealized>=0 %}pos{% else %}neg{% endif %}" style="font-size:18px;">
              {% if unrealized>=0 %}+{% endif %}${{ "%.0f"|format(unrealized) }}
            </span>
          </div>
        </div>
        
        <form action="/close" method="post" onsubmit="return confirm('WARNING: Are you sure you want to close this position at current market price?')">
          <button type="submit" class="btn-close">Emergency Close Position</button>
        </form>
      {% else %}
        <div style="display:flex; flex-direction:column; align-items:center; justify-content:center; height:180px; text-align:center;">
          <div style="font-size:32px; margin-bottom:12px; opacity:0.5;">⚖️</div>
          <div style="font-size:18px; font-weight:700; color:var(--text-muted);">Flat Market</div>
          <div style="font-size:13px; color:rgba(148, 163, 184, 0.6); margin-top:4px;">Waiting for TradingView signal</div>
        </div>
      {% endif %}
    </div>

    <div class="card">
      <h2>XAU/USD Market</h2>
      {% if last_price %}
        <div style="margin-bottom:16px;">
          <span class="status-dot status-connected"></span>
          <span style="font-size:12px; font-weight:600; color:var(--color-win);">OANDA Connected</span>
        </div>
        <div style="font-size:42px; font-weight:800; color:#fff; margin-bottom:16px; letter-spacing:-1px;">
          {{ "%.2f"|format(last_price.mid) }}
        </div>
        <div class="level-row"><span class="stat-label">Ask (Buy)</span><span class="stat-val">{{ "%.2f"|format(last_price.ask) }}</span></div>
        <div class="level-row"><span class="stat-label">Bid (Sell)</span><span class="stat-val">{{ "%.2f"|format(last_price.bid) }}</span></div>
      {% else %}
        <div style="margin-bottom:16px;">
          <span class="status-dot status-error"></span>
          <span style="font-size:12px; font-weight:600; color:var(--color-loss);">Disconnected</span>
        </div>
        <div style="color:var(--text-muted); font-size:13px; line-height:1.6;">
          Waiting for API response.<br>Check Railway logs if this persists.
        </div>
      {% endif %}
    </div>
  </div>

  <div class="table-container">
    <div class="table-header">Recent Trade History (Last 100)</div>
    {% if trades %}
    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>Side</th>
          <th>Entry</th>
          <th>SL (Raw → Adj)</th>
          <th>TP1 (Raw → Adj)</th>
          <th>RR</th>
          <th>Status</th>
          <th>P&L</th>
          <th>Date (UTC)</th>
        </tr>
      </thead>
      <tbody>
      {% for t in trades %}
        <tr>
          <td style="color:var(--text-muted);">#{{ t.id }}</td>
          <td class="{% if t.action=='LONG' %}pos{% else %}neg{% endif %}" style="font-weight:700;">{{ t.action }}</td>
          <td>{{ "%.2f"|format(t.entry) }}</td>
          <td style="color:var(--color-loss); opacity:0.9;">{{ "%.2f"|format(t.sl_raw) }} <span style="color:var(--text-muted);">→</span> {{ "%.2f"|format(t.sl) }}</td>
          <td style="color:var(--color-win); opacity:0.9;">{{ "%.2f"|format(t.tp1_raw) }} <span style="color:var(--text-muted);">→</span> {{ "%.2f"|format(t.tp1) }}</td>
          <td style="color:var(--text-muted);">{{ "%.2f"|format(t.rr) }}</td>
          <td><span class="badge badge-{{ t.status }}">{{ t.status }}</span></td>
          <td class="{% if t.pnl>0 %}pos{% elif t.pnl<0 %}neg{% else %}neu{% endif %}" style="font-weight:800; font-size:15px;">
            {% if t.status=='OPEN' %}—{% elif t.pnl>=0 %}+${{ "%.0f"|format(t.pnl) }}{% else %}-${{ "%.0f"|format(t.pnl|abs) }}{% endif %}
          </td>
          <td style="color:var(--text-muted); font-size:12px;">{{ t.opened_at[:16].replace('T', ' ') if t.opened_at else '—' }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    {% else %}
      <div style="padding: 40px; text-align: center; color: var(--text-muted); font-weight: 500;">
        No trades registered yet. Waiting for webhook signals.
      </div>
    {% endif %}
  </div>

</div>
</body>
</html>
"""

@app.route("/")
def dashboard():
    now         = datetime.now(timezone.utc)
    month_start = f"{now.year}-{now.month:02d}-01"
    with get_db() as conn:
        month_rows = conn.execute(
            "SELECT * FROM trades WHERE status != 'OPEN' AND opened_at >= ? ORDER BY opened_at DESC",
            (month_start,)).fetchall()
        all_rows = conn.execute(
            "SELECT * FROM trades ORDER BY opened_at DESC LIMIT 100").fetchall()
    closed  = [dict(r) for r in month_rows]
    all_t   = [dict(r) for r in all_rows]
    total   = sum(t["pnl"] for t in closed)
    wins    = sum(1 for t in closed if t["status"] == "WIN")
    losses  = sum(1 for t in closed if t["status"] == "LOSS")
    count   = len(closed)
    wr      = round(wins / count * 100) if count > 0 else 0
    best    = max((t["pnl"] for t in closed), default=0)
    worst   = min((t["pnl"] for t in closed), default=0)
    skipped = sum(1 for t in all_t if t["status"] not in ("WIN","LOSS","OPEN","MANUAL"))
    with lock:
        cur_trade = open_trade
        cur_price = last_price
    unrealized = 0.0
    if cur_trade and cur_price:
        unrealized = calc_pnl(cur_trade["action"], cur_trade["entry"], cur_price["mid"], OZ_FULL)
    return render_template_string(DASHBOARD,
        month=now.strftime("%b %Y"), total=total, wins=wins, losses=losses,
        count=count, win_rate=wr, best=best, worst=worst, skipped=skipped,
        open_trade=cur_trade, unrealized=unrealized, last_price=cur_price,
        trades=all_t, lot_size=LOT_SIZE, slippage=SLIPPAGE, min_rr=MIN_RR)

@app.route("/status")
def status():
    with lock:
        return jsonify({
            "oanda_connected": last_price is not None,
            "open_trade": open_trade,
            "last_price": last_price,
            "config": {"slippage": SLIPPAGE, "min_rr": MIN_RR, "lot_size": LOT_SIZE,
                       "account_id_set": bool(ACCOUNT_ID), "token_set": bool(OANDA_TOKEN)}
        })

init_db()
monitor_thread = threading.Thread(target=price_monitor, daemon=True)
monitor_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
