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
MAX_DIST    = float(os.environ.get("MAX_DIST",  "50.0")) # Maksymalny dozwolony dystans do TP1 i SL
DAILY_GOAL  = float(os.environ.get("DAILY_GOAL","500"))
OZ_FULL     = LOT_SIZE * 100
DB_PATH     = "trades.db"

lock            = threading.Lock()
open_trade      = None
last_price      = None
trading_enabled = True
recent_ticks    = []
session_high    = -float('inf')
session_low     =  float('inf')


def get_price():
    headers = {"Authorization": f"Bearer {OANDA_TOKEN}"}
    r = requests.get(
        f"https://api-fxpractice.oanda.com/v3/accounts/{ACCOUNT_ID}/pricing",
        headers=headers, params={"instruments": INSTRUMENT}, timeout=10)
    r.raise_for_status()
    data = r.json()["prices"][0]
    return float(data["bids"][0]["price"]), float(data["asks"][0]["price"])

def adjust_levels(action, tp1, sl):
    if action == "LONG": return tp1 - SLIPPAGE, sl - SLIPPAGE
    return tp1 + SLIPPAGE, sl + SLIPPAGE

def calc_rr(action, entry, adj_tp1, adj_sl):
    if action == "LONG": reward, risk = adj_tp1 - entry, entry - adj_sl
    else:                reward, risk = entry - adj_tp1, adj_sl - entry
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
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL, entry REAL NOT NULL,
                sl_raw REAL NOT NULL, tp1_raw REAL NOT NULL,
                sl REAL NOT NULL, tp1 REAL NOT NULL,
                rr REAL DEFAULT 0, score TEXT DEFAULT '',
                status TEXT DEFAULT 'OPEN',
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
                direction = "FLAT"
                if last_price:
                    if mid > last_price["mid"]:   direction = "UP"
                    elif mid < last_price["mid"]: direction = "DOWN"
                last_price = {"bid": bid, "ask": ask, "mid": mid, "spread": round(ask - bid, 2)}
                session_high = max(session_high, mid)
                session_low  = min(session_low,  mid)
                if direction != "FLAT":
                    recent_ticks.insert(0, {"time": now_time_str, "price": mid, "direction": direction})
                    if len(recent_ticks) > 8: recent_ticks.pop()
                if open_trade is not None:
                    t      = open_trade
                    action = t["action"]
                    tp1_hit = ask >= t["tp1"] if action == "LONG" else bid <= t["tp1"]
                    sl_hit  = bid <= t["sl"]  if action == "LONG" else ask >= t["sl"]
                    if tp1_hit or sl_hit:
                        exit_p = t["tp1"] if tp1_hit else t["sl"]
                        status = "WIN" if tp1_hit else "LOSS"
                        pnl    = round(calc_pnl(action, t["entry"], exit_p, OZ_FULL), 2)
                        now_s  = datetime.now(timezone.utc).isoformat()
                        with get_db() as conn:
                            conn.execute("UPDATE trades SET status=?, pnl=?, closed_at=? WHERE id=?",
                                         (status, pnl, now_s, t["id"]))
                            conn.commit()
                        print(f"[Trade #{t['id']}] {status}  ${pnl:.2f}")
                        open_trade = None
            time.sleep(2)
        except requests.exceptions.HTTPError as e:
            print(f"[Monitor] HTTP error: {e.response.status_code}")
            time.sleep(15)
        except Exception as e:
            print(f"[Monitor] Error: {e}")
            time.sleep(10)


@app.route("/webhook", methods=["POST"])
def webhook():
    global open_trade
    try:
        data   = request.get_json(force=True)
        action = str(data.get("action", "")).upper()
        if action not in ("LONG", "SHORT"):
            return jsonify({"error": "invalid action"}), 400
            
        entry, sl_raw, tp1_raw = float(data["entry"]), float(data["sl"]), float(data["tp1"])
        score  = str(data.get("score", ""))
        adj_tp1, adj_sl = adjust_levels(action, tp1_raw, sl_raw)
        rr = calc_rr(action, entry, adj_tp1, adj_sl)

        risk_dist = abs(entry - sl_raw)
        reward_dist = abs(tp1_raw - entry)

        if not trading_enabled:
            return jsonify({"status": "skipped", "reason": "trading disabled"}), 200
        if rr < MIN_RR:
            return jsonify({"status": "skipped", "reason": f"RR {rr:.2f} too low"}), 200
        if risk_dist > MAX_DIST or reward_dist > MAX_DIST:
            return jsonify({"status": "skipped", "reason": f"Distance > {MAX_DIST} pts limit"}), 200

        with lock:
            if open_trade is not None:
                return jsonify({"status": "skipped", "reason": "already in trade"}), 200
            now_s = datetime.now(timezone.utc).isoformat()
            with get_db() as conn:
                cur = conn.execute(
                    "INSERT INTO trades (action,entry,sl_raw,tp1_raw,sl,tp1,rr,score,status,opened_at)"
                    " VALUES (?,?,?,?,?,?,?,?,'OPEN',?)",
                    (action, entry, sl_raw, tp1_raw, adj_sl, adj_tp1, round(rr, 2), score, now_s))
                trade_id = cur.lastrowid
                conn.commit()
            open_trade = {"id": trade_id, "action": action, "entry": entry,
                          "sl": adj_sl, "tp1": adj_tp1, "rr": round(rr, 2)}
        print(f"[Trade #{trade_id}] {action} @ {entry}  SL:{adj_sl}  TP1:{adj_tp1}  RR:{rr:.2f}")
        return jsonify({"status": "ok", "trade_id": trade_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/close", methods=["POST"])
def manual_close():
    global open_trade
    with lock:
        if open_trade is None: return jsonify({"status": "no open trade"}), 200
        price = last_price["mid"] if last_price else open_trade["entry"]
        pnl   = round(calc_pnl(open_trade["action"], open_trade["entry"], price, OZ_FULL), 2)
        now_s = datetime.now(timezone.utc).isoformat()
        with get_db() as conn:
            conn.execute("UPDATE trades SET status='MANUAL', pnl=?, closed_at=? WHERE id=?",
                         (pnl, now_s, open_trade["id"]))
            conn.commit()
        open_trade = None
    return jsonify({"status": "closed"})


@app.route("/toggle", methods=["POST"])
def toggle():
    global trading_enabled
    with lock:
        trading_enabled = not trading_enabled
    state = "ENABLED" if trading_enabled else "DISABLED"
    print(f"[Bot] Trading {state}")
    return jsonify({"trading_enabled": trading_enabled})


# ─────────────────────────────────────────────────────────────────────────────
DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Apex Paper Bot | Command Center</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
  :root {
    --bg-dark:#0a0c14; --bg-card:rgba(22,27,46,0.65); --border:rgba(255,255,255,0.06);
    --text:#e2e8f0; --muted:#94a3b8;
    --win:#10b981; --win-bg:rgba(16,185,129,0.15);
    --loss:#f43f5e; --loss-bg:rgba(244,63,94,0.15);
    --open:#3b82f6; --open-bg:rgba(59,130,246,0.15);
    --manual:#8b5cf6; --manual-bg:rgba(139,92,246,0.15);
  }
  *{margin:0;padding:0;box-sizing:border-box;font-family:'Inter',sans-serif}
  body{background:radial-gradient(circle at top right,#151a30,var(--bg-dark));color:var(--text);font-size:14px;min-height:100vh;padding-bottom:50px}
  .wrap{max-width:1400px;margin:0 auto;padding:20px}
  .hdr{display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:20px;flex-wrap:wrap;gap:16px}
  .hdr h1{font-size:26px;font-weight:800;background:linear-gradient(to right,#fff,#94a3b8);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
  .hdr p{color:var(--muted);font-size:13px;margin-top:4px}
  .tags{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  .tag{background:rgba(255,255,255,0.03);border:1px solid var(--border);padding:4px 10px;border-radius:20px;font-size:11px;font-weight:600;color:var(--muted)}
  .card{background:var(--bg-card);backdrop-filter:blur(12px);border:1px solid var(--border);border-radius:12px;padding:20px;box-shadow:0 10px 30px -10px rgba(0,0,0,0.5)}
  .card h2{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:16px;font-weight:700}
  .top-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px;margin-bottom:20px}
  .pnl-big{font-size:36px;font-weight:800;margin-bottom:16px;letter-spacing:-1px}
  .sr{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid rgba(255,255,255,0.04)}
  .sr:last-child{border-bottom:none}
  .sk{color:var(--muted);font-weight:500}.sv{font-weight:700}
  .pos{color:var(--win)}.neg{color:var(--loss)}.neu{color:var(--muted)}
  .streak-badge{display:inline-block;padding:2px 8px;border-radius:20px;font-size:12px;font-weight:700}
  .streak-w{background:rgba(16,185,129,0.15);color:#10b981}
  .streak-l{background:rgba(244,63,94,0.15);color:#f43f5e}
  .danger-wrap{width:100%;height:10px;background:rgba(0,0,0,0.4);border-radius:5px;margin:16px 0;position:relative;overflow:hidden;border:1px solid var(--border)}
  .danger-fill{height:100%;background:linear-gradient(90deg,var(--loss),var(--win));position:absolute;left:0;width:50%}
  .danger-marker{width:4px;height:14px;background:#fff;box-shadow:0 0 8px #fff;position:absolute;top:-2px;border-radius:2px;transform:translateX(-50%);z-index:2;transition:left 0.3s}
  .price-text{font-size:42px;font-weight:800;letter-spacing:-1px;transition:color 0.2s;margin-bottom:16px;color:#fff}
  .flash-up{color:var(--win)!important;text-shadow:0 0 15px rgba(16,185,129,0.4)}
  .flash-down{color:var(--loss)!important;text-shadow:0 0 15px rgba(244,63,94,0.4)}
  .btn-close{width:100%;background:var(--loss-bg);color:var(--loss);border:1px solid rgba(244,63,94,0.3);padding:12px;border-radius:8px;font-weight:700;cursor:pointer;transition:all 0.2s;margin-top:16px}
  .btn-close:hover{background:var(--loss);color:#fff}
  /* Toggle button */
  .btn-toggle{padding:8px 18px;border-radius:8px;font-weight:700;font-size:13px;cursor:pointer;transition:all 0.2s;border:none;letter-spacing:0.3px}
  .btn-toggle-on{background:rgba(16,185,129,0.15);color:#10b981;border:1px solid rgba(16,185,129,0.3)}
  .btn-toggle-on:hover{background:#10b981;color:#fff}
  .btn-toggle-off{background:rgba(244,63,94,0.15);color:#f43f5e;border:1px solid rgba(244,63,94,0.3)}
  .btn-toggle-off:hover{background:#f43f5e;color:#fff}
  .toggle-status{display:flex;align-items:center;gap:8px;font-size:12px;font-weight:600}
  .dot-pulse{width:8px;height:8px;border-radius:50%;display:inline-block}
  .dot-on{background:#10b981;animation:pulse 2s infinite}
  .dot-off{background:#f43f5e}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:0.35}}
  .cmd-grid{display:grid;grid-template-columns:2fr 1fr;gap:20px;margin-bottom:20px}
  @media(max-width:900px){.cmd-grid{grid-template-columns:1fr}}
  .tick-wrap{display:flex;flex-direction:column;gap:10px;min-height:220px}
  .tick-item{display:flex;justify-content:space-between;align-items:center;background:rgba(0,0,0,0.2);padding:12px 16px;border-radius:8px;border-left:4px solid transparent;animation:slideIn 0.3s ease-out;font-size:15px}
  @keyframes slideIn{from{opacity:0;transform:translateY(-10px)}to{opacity:1;transform:translateY(0)}}
  .tick-up{border-left-color:var(--win)}.tick-down{border-left-color:var(--loss)}
  .target-bg{width:100%;background:rgba(0,0,0,0.4);height:16px;border-radius:8px;overflow:hidden;border:1px solid var(--border)}
  .target-fill{height:100%;background:linear-gradient(90deg,var(--open),var(--win));transition:width 0.5s}
  .tbl-wrap{background:var(--bg-card);backdrop-filter:blur(12px);border:1px solid var(--border);border-radius:16px;overflow-x:auto;margin-top:20px}
  .tbl-hdr{padding:16px 20px;border-bottom:1px solid var(--border);font-size:14px;font-weight:700}
  table{width:100%;border-collapse:collapse;text-align:left}
  th{font-size:11px;color:var(--muted);text-transform:uppercase;padding:12px 20px;background:rgba(0,0,0,0.2);font-weight:700}
  td{padding:12px 20px;border-bottom:1px solid rgba(255,255,255,0.03);font-weight:500;font-size:13px}
  tr:last-child td{border-bottom:none}
  tr:hover td{background:rgba(255,255,255,0.02)}
  .badge{padding:4px 8px;border-radius:4px;font-size:10px;font-weight:700;text-transform:uppercase}
  .badge-WIN{background:var(--win-bg);color:var(--win)}
  .badge-LOSS{background:var(--loss-bg);color:var(--loss)}
  .badge-OPEN{background:var(--open-bg);color:var(--open)}
  .badge-MANUAL{background:var(--manual-bg);color:var(--manual)}
</style>
</head>
<body>
<div class="wrap">

  <div class="hdr">
    <div>
      <h1>Apex Live Terminal</h1>
      <p>{{ month }} &mdash; Real-Time Command Center</p>
    </div>
    <div class="tags">
      <span class="tag">{{ lot_size }} Lot = ${{ (lot_size*100)|int }}/pt</span>
      <span class="tag">100% Exit at TP1</span>
      <span class="tag">Max {{ max_dist }}pt Dist</span>
      <span class="tag">Min {{ min_rr }}:1 RR</span>
      <div id="toggle-wrap" style="display:flex;align-items:center;gap:10px;margin-left:8px">
        <div class="toggle-status">
          <span class="dot-pulse" id="trading-dot"></span>
          <span id="trading-label"></span>
        </div>
        <button class="btn-toggle" id="toggle-btn" onclick="toggleTrading()">...</button>
      </div>
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
      <div class="sr"><span class="sk">Trades</span><span class="sv">{{ count }}</span></div>
      <div class="sr"><span class="sk">Win Rate</span><span class="sv {% if win_rate >= 50 %}pos{% else %}neu{% endif %}">{{ win_rate }}%</span></div>
      <div class="sr"><span class="sk">Wins / Losses</span><span class="sv"><span class="pos">{{ wins }}</span> / <span class="neg">{{ losses }}</span></span></div>
      <div class="sr"><span class="sk">Best Trade</span><span class="sv pos">{% if best > 0 %}+${{ "%.0f"|format(best) }}{% else %}&mdash;{% endif %}</span></div>
      <div class="sr"><span class="sk">Avg Win</span><span class="sv pos">{% if avg_win > 0 %}+${{ "%.0f"|format(avg_win) }}{% else %}&mdash;{% endif %}</span></div>
      <div class="sr"><span class="sk">Avg Loss</span><span class="sv neg">{% if avg_loss < 0 %}-${{ "%.0f"|format(avg_loss|abs) }}{% else %}&mdash;{% endif %}</span></div>
      <div class="sr">
        <span class="sk">Profit Factor</span>
        <span class="sv {% if pf is not none and pf >= 1.5 %}pos{% elif pf is not none and pf >= 1.0 %}neu{% elif pf is not none %}neg{% else %}pos{% endif %}">
          {% if pf is not none %}{{ "%.2f"|format(pf) }}{% else %}&infin;{% endif %}
        </span>
      </div>
      <div class="sr">
        <span class="sk">Streak</span>
        <span>
          {% if streak > 0 %}<span class="streak-badge streak-w">{{ streak }}W &#128293;</span>
          {% elif streak < 0 %}<span class="streak-badge streak-l">{{ streak|abs }}L</span>
          {% else %}<span class="neu">&mdash;</span>{% endif %}
        </span>
      </div>
    </div>

    <div class="card">
      <h2>Active Position</h2>
      <div id="trade-ui">
        <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:150px;text-align:center">
          <div style="font-size:24px;margin-bottom:8px;opacity:0.5">&#9203;</div>
          <div style="font-size:16px;font-weight:700;color:var(--muted)">Fetching...</div>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>XAU/USD Live Price</h2>
      <div id="conn-status" style="font-size:12px;font-weight:600;color:var(--muted);margin-bottom:12px">Connecting...</div>
      <div id="price-main" class="price-text">--.--</div>
      <div class="sr"><span class="sk">Ask (Buy)</span><span class="sv" id="price-ask">--.--</span></div>
      <div class="sr"><span class="sk">Bid (Sell)</span><span class="sv" id="price-bid">--.--</span></div>
    </div>
  </div>

  <div class="cmd-grid">
    <div class="card">
      <h2>Live Tick Tape</h2>
      <div class="tick-wrap" id="tick-tape">
        <div style="color:var(--muted);font-size:13px;padding:20px 0">Waiting for price movements...</div>
      </div>
    </div>
    <div class="card" style="display:flex;flex-direction:column;justify-content:space-between">
      <div>
        <h2>Market Pulse</h2>
        <div class="sr"><span class="sk">Live Spread</span><span class="sv" id="live-spread" style="color:var(--open)">-- pts</span></div>
        <div class="sr"><span class="sk">Session High</span><span class="sv" id="sess-high">--.--</span></div>
        <div class="sr"><span class="sk">Session Low</span><span class="sv" id="sess-low">--.--</span></div>
      </div>
      <div style="margin-top:15px">
        <div style="display:flex;justify-content:space-between;margin-bottom:8px">
          <h2 style="margin:0">Daily Target</h2>
          <span style="font-size:12px;font-weight:700" class="{% if daily_pnl >= 0 %}pos{% else %}neg{% endif %}">
            ${{ "%.0f"|format(daily_pnl) }} / ${{ "%.0f"|format(daily_goal) }}
          </span>
        </div>
        <div class="target-bg"><div class="target-fill" style="width:{{ target_pct }}%"></div></div>
      </div>
    </div>
  </div>

  <div class="tbl-wrap">
    <div class="tbl-hdr">Trade History</div>
    {% if trades %}
    <table>
      <thead><tr>
        <th>#</th><th>Side</th><th>Entry</th><th>SL (adj)</th><th>TP1 (adj)</th>
        <th>RR</th><th>Score</th><th>Status</th><th>P&amp;L</th><th>Date (UTC)</th>
      </tr></thead>
      <tbody>
      {% for t in trades %}
        <tr>
          <td style="color:var(--muted)">#{{ t.id }}</td>
          <td class="{% if t.action=='LONG' %}pos{% else %}neg{% endif %}" style="font-weight:700">{{ t.action }}</td>
          <td>{{ "%.2f"|format(t.entry) }}</td>
          <td style="color:var(--loss)">{{ "%.2f"|format(t.sl) }}</td>
          <td style="color:var(--win)">{{ "%.2f"|format(t.tp1) }}</td>
          <td style="color:var(--muted)">{{ "%.2f"|format(t.rr) }}</td>
          <td style="color:var(--muted)">{{ t.score }}</td>
          <td><span class="badge badge-{{ t.status }}">{{ t.status }}</span></td>
          <td class="{% if t.pnl>0 %}pos{% elif t.pnl<0 %}neg{% else %}neu{% endif %}" style="font-weight:800">
            {% if t.status=='OPEN' %}&mdash;{% elif t.pnl>=0 %}+${{ "%.0f"|format(t.pnl) }}{% else %}-${{ "%.0f"|format(t.pnl|abs) }}{% endif %}
          </td>
          <td style="color:var(--muted)">{{ t.opened_at[:16].replace('T',' ') if t.opened_at else '&mdash;' }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    {% else %}
    <div style="padding:30px;text-align:center;color:var(--muted)">No trades yet.</div>
    {% endif %}
  </div>
</div>

<script>
  let prevPrice = 0, activeId = null;
  const lotSize = {{ lot_size }};

  function updateToggleUI(enabled) {
    const dot = document.getElementById('trading-dot');
    const lbl = document.getElementById('trading-label');
    const btn = document.getElementById('toggle-btn');
    if (enabled) {
      dot.className = 'dot-pulse dot-on';
      lbl.innerText = 'Trading ON';
      lbl.style.color = 'var(--win)';
      btn.className = 'btn-toggle btn-toggle-on';
      btn.innerText = 'Pause Bot';
    } else {
      dot.className = 'dot-pulse dot-off';
      lbl.innerText = 'Trading OFF';
      lbl.style.color = 'var(--loss)';
      btn.className = 'btn-toggle btn-toggle-off';
      btn.innerText = 'Resume Bot';
    }
  }

  function toggleTrading() {
    fetch('/toggle', {method:'POST'}).then(r=>r.json()).then(d=>{
      updateToggleUI(d.trading_enabled);
    });
  }

  function update() {
    fetch('/status').then(r=>r.json()).then(d=>{
      updateToggleUI(d.trading_enabled);

      if (!d.last_price || !d.last_price.mid) return;
      document.getElementById('conn-status').innerHTML = '<span style="color:var(--win)">&#9679; API Connected</span>';

      const p  = d.last_price.mid;
      const el = document.getElementById('price-main');
      el.innerText = p.toFixed(2);
      document.getElementById('price-ask').innerText = d.last_price.ask.toFixed(2);
      document.getElementById('price-bid').innerText = d.last_price.bid.toFixed(2);

      if (p > prevPrice && prevPrice !== 0) {
        el.classList.add('flash-up'); setTimeout(()=>el.classList.remove('flash-up'), 500);
      } else if (p < prevPrice && prevPrice !== 0) {
        el.classList.add('flash-down'); setTimeout(()=>el.classList.remove('flash-down'), 500);
      }
      prevPrice = p;

      document.getElementById('live-spread').innerText = d.last_price.spread.toFixed(2) + ' pts';
      if (d.session_high) document.getElementById('sess-high').innerText = d.session_high.toFixed(2);
      if (d.session_low)  document.getElementById('sess-low').innerText  = d.session_low.toFixed(2);

      if (d.recent_ticks && d.recent_ticks.length > 0) {
        const tape = document.getElementById('tick-tape');
        tape.innerHTML = '';
        d.recent_ticks.forEach(tick => {
          const row = document.createElement('div');
          row.className = 'tick-item ' + (tick.direction==='UP' ? 'tick-up' : 'tick-down');
          const icon = tick.direction==='UP' ? '&#9650;' : '&#9660;';
          const cls  = tick.direction==='UP' ? 'pos' : 'neg';
          row.innerHTML = `<span style="color:var(--muted);font-size:12px">${tick.time} UTC</span><span class="${cls}" style="font-weight:700">${icon} ${tick.price.toFixed(2)}</span>`;
          tape.appendChild(row);
        });
      }

      const ui = document.getElementById('trade-ui');
      if (d.open_trade) {
        const t = d.open_trade;
        const isLong = t.action === 'LONG';
        if (activeId !== t.id && activeId !== null) window.location.reload();
        activeId = t.id;
        const pnl  = isLong ? (p - t.entry)*(lotSize*100) : (t.entry - p)*(lotSize*100);
        const cls  = pnl >= 0 ? 'pos' : 'neg';
        const sign = pnl >= 0 ? '+' : '';
        const range = Math.abs(t.tp1 - t.sl);
        const pct   = Math.max(0, Math.min(100, (Math.abs(p - t.sl) / range) * 100));
        ui.innerHTML = `
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
            <span style="font-weight:800;font-size:22px" class="${isLong?'pos':'neg'}">${t.action}</span>
            <span class="badge badge-OPEN">RR ${t.rr}</span>
          </div>
          <div style="display:flex;justify-content:space-between;font-size:11px;color:#94a3b8">
            <span>SL: ${t.sl.toFixed(2)}</span><span>TP1: ${t.tp1.toFixed(2)}</span>
          </div>
          <div class="danger-wrap">
            <div class="danger-fill"></div>
            <div class="danger-marker" style="left:${pct}%"></div>
          </div>
          <div class="sr"><span class="sk">Entry</span><span class="sv">${t.entry.toFixed(2)}</span></div>
          <div class="sr"><span class="sk">Unrealized P&L</span><span class="sv ${cls}" style="font-size:18px">${sign}$${pnl.toFixed(2)}</span></div>
          <button class="btn-close" onclick="closeT()">Emergency Close</button>
        `;
      } else {
        if (activeId !== null) { setTimeout(()=>window.location.reload(), 1500); activeId = null; }
        ui.innerHTML = `<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:150px;text-align:center"><div style="font-size:28px;margin-bottom:8px;opacity:0.5">&#9878;</div><div style="font-size:16px;font-weight:700;color:var(--muted)">Flat Market</div></div>`;
      }
    }).catch(()=>{});
  }

  function closeT() {
    if (confirm('Close position at current market price?')) {
      fetch('/close',{method:'POST'}).then(()=>window.location.reload());
    }
  }

  setInterval(update, 2000);
  update();
</script>
</body>
</html>"""


@app.route("/")
def dashboard():
    now         = datetime.now(timezone.utc)
    month_start = f"{now.year}-{now.month:02d}-01"
    today_start = f"{now.year}-{now.month:02d}-{now.day:02d}"

    with get_db() as conn:
        month_rows = conn.execute(
            "SELECT * FROM trades WHERE status != 'OPEN' AND opened_at >= ? ORDER BY opened_at DESC",
            (month_start,)).fetchall()
        all_rows = conn.execute(
            "SELECT * FROM trades ORDER BY opened_at DESC LIMIT 50").fetchall()

    closed = [dict(r) for r in month_rows]
    all_t  = [dict(r) for r in all_rows]

    total  = sum(t["pnl"] for t in closed)
    wins   = sum(1 for t in closed if t["status"] == "WIN")
    losses = sum(1 for t in closed if t["status"] == "LOSS")
    count  = len(closed)
    wr     = round(wins / count * 100) if count > 0 else 0
    best   = max((t["pnl"] for t in closed), default=0)

    win_trades  = [t for t in closed if t["status"] == "WIN"]
    loss_trades = [t for t in closed if t["status"] == "LOSS"]
    avg_win   = round(sum(t["pnl"] for t in win_trades)  / len(win_trades),  2) if win_trades  else 0
    avg_loss  = round(sum(t["pnl"] for t in loss_trades) / len(loss_trades), 2) if loss_trades else 0
    gross_win  = sum(t["pnl"] for t in win_trades)
    gross_loss = abs(sum(t["pnl"] for t in loss_trades))
    pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else None

    streak = 0
    if closed:
        ref = closed[0]["status"] if closed[0]["status"] in ("WIN", "LOSS") else None
        if ref:
            for t in closed:
                if t["status"] == ref: streak += 1 if ref == "WIN" else -1
                else: break

    today_rows = [t for t in closed if t.get("closed_at") and t["closed_at"] >= today_start]
    daily_pnl  = sum(t["pnl"] for t in today_rows)
    target_pct = min(100, max(0, (daily_pnl / DAILY_GOAL) * 100)) if DAILY_GOAL > 0 else 0

    return render_template_string(DASHBOARD,
        month=now.strftime("%b %Y"), total=total, wins=wins, losses=losses,
        count=count, win_rate=wr, best=best, trades=all_t,
        avg_win=avg_win, avg_loss=avg_loss, pf=pf, streak=streak,
        lot_size=LOT_SIZE, slippage=SLIPPAGE, min_rr=MIN_RR,
        daily_pnl=daily_pnl, daily_goal=DAILY_GOAL, target_pct=target_pct,
        max_dist=MAX_DIST)


@app.route("/status")
def status():
    with lock:
        safe_high = session_high if session_high != -float('inf') else None
        safe_low  = session_low  if session_low  !=  float('inf') else None
        return jsonify({
            "last_price":      last_price,
            "recent_ticks":    recent_ticks,
            "session_high":    safe_high,
            "session_low":     safe_low,
            "open_trade":      open_trade,
            "trading_enabled": trading_enabled
        })


init_db()
monitor_thread = threading.Thread(target=price_monitor, daemon=True)
monitor_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
