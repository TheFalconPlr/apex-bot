import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template_string

try:
    import oandapyV20
    import oandapyV20.endpoints.pricing as pricing
    OANDA_OK = True
except ImportError:
    OANDA_OK = False

app = Flask(__name__)

OANDA_TOKEN = os.environ.get("OANDA_TOKEN", "")
ACCOUNT_ID  = os.environ.get("ACCOUNT_ID",  "")
INSTRUMENT  = "XAU_USD"
LOT_SIZE    = float(os.environ.get("LOT_SIZE", "0.5"))
OZ_FULL     = LOT_SIZE * 100
OZ_HALF     = OZ_FULL * 0.5
DB_PATH     = "trades.db"

lock       = threading.Lock()
open_trade = None
last_price = None

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="15">
<title>Apex Paper Bot — {{ month }}</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#0b0d17; color:#c8cad8; font-family:'Segoe UI',sans-serif; font-size:14px; }
  .top { display:flex; gap:16px; padding:20px; flex-wrap:wrap; }
  .card { background:#13162a; border:1px solid #222640; border-radius:10px; padding:18px 22px; }
  .card h2 { font-size:11px; color:#6b6f8e; text-transform:uppercase; letter-spacing:1px; margin-bottom:10px; }
  .pnl-big { font-size:36px; font-weight:700; margin-bottom:14px; }
  .pos { color:#1dce8a; } .neg { color:#e24b4a; } .neu { color:#8890b0; }
  .stat-row { display:flex; justify-content:space-between; padding:5px 0; border-bottom:1px solid #1a1d33; }
  .stat-row:last-child { border-bottom:none; }
  .stat-label { color:#6b6f8e; } .stat-val { font-weight:600; }
  .trade-dir { font-size:22px; font-weight:700; margin-bottom:12px; }
  .level-row { display:flex; justify-content:space-between; padding:4px 0; font-size:13px; }
  .flat-msg { color:#4a4e6a; font-size:16px; margin-top:10px; }
  .price-big { font-size:26px; font-weight:700; color:#e8e9f0; margin-bottom:6px; }
  .long-dir { color:#1dce8a; } .short-dir { color:#e24b4a; }
  .history { padding:0 20px 30px; }
  .history h3 { font-size:12px; color:#6b6f8e; text-transform:uppercase; letter-spacing:1px; margin-bottom:12px; }
  table { width:100%; border-collapse:collapse; }
  th { font-size:11px; color:#6b6f8e; text-transform:uppercase; padding:8px 12px; text-align:left; background:#0f1121; border-bottom:1px solid #1e2140; }
  td { padding:9px 12px; border-bottom:1px solid #141728; font-size:13px; }
  tr:hover td { background:#141930; }
  .badge { display:inline-block; padding:2px 8px; border-radius:4px; font-size:11px; font-weight:700; text-transform:uppercase; }
  .badge-WIN     { background:#0d3d2a; color:#1dce8a; }
  .badge-LOSS    { background:#3d1212; color:#e24b4a; }
  .badge-PARTIAL { background:#3d2d10; color:#f09820; }
  .badge-OPEN    { background:#1a2060; color:#7090ff; }
  .badge-MANUAL  { background:#2a2a3a; color:#8890b0; }
  .tp1-badge { font-size:10px; background:#1a3040; color:#60b0f0; padding:1px 5px; border-radius:3px; margin-left:6px; }
  .refresh-note { font-size:11px; color:#3a3e55; padding:10px 20px 0; }
</style>
</head>
<body>
<div style="padding:20px 20px 0;display:flex;align-items:center;gap:12px;">
  <div style="font-size:20px;font-weight:700;color:#e8e9f0;">Apex Paper Bot</div>
  <div style="font-size:13px;color:#6b6f8e;">{{ month }} — paper trading only</div>
</div>
<div class="refresh-note">Auto-refreshes every 15 seconds</div>
<div class="top">

  <div class="card" style="min-width:220px;">
    <h2>{{ month }} P&L</h2>
    {% if total >= 0 %}
      <div class="pnl-big pos">+${{ "%.0f"|format(total) }}</div>
    {% else %}
      <div class="pnl-big neg">-${{ "%.0f"|format(total|abs) }}</div>
    {% endif %}
    <div class="stat-row"><span class="stat-label">Trades</span><span class="stat-val">{{ count }}</span></div>
    <div class="stat-row"><span class="stat-label">Wins</span><span class="stat-val pos">{{ wins }}</span></div>
    <div class="stat-row"><span class="stat-label">Partials</span><span class="stat-val" style="color:#f09820;">{{ partial }}</span></div>
    <div class="stat-row"><span class="stat-label">Losses</span><span class="stat-val neg">{{ losses }}</span></div>
    <div class="stat-row"><span class="stat-label">Win rate</span><span class="stat-val">{{ win_rate }}%</span></div>
    <div class="stat-row"><span class="stat-label">Best trade</span><span class="stat-val pos">{% if best > 0 %}+${{ "%.0f"|format(best) }}{% else %}—{% endif %}</span></div>
    <div class="stat-row"><span class="stat-label">Worst trade</span><span class="stat-val neg">{% if worst < 0 %}-${{ "%.0f"|format(worst|abs) }}{% else %}—{% endif %}</span></div>
    <div class="stat-row" style="margin-top:4px;">
      <span class="stat-label" style="font-size:11px;color:#3a3e55;">{{ lot_size }} lot = ${{ "%.0f"|format(lot_size*100) }}/pt</span>
      <span class="stat-label" style="font-size:11px;color:#3a3e55;">50% TP1 / 50% TP2</span>
    </div>
  </div>

  <div class="card" style="min-width:260px;">
    <h2>Open Trade</h2>
    {% if open_trade %}
      <div class="trade-dir {% if open_trade.action=='LONG' %}long-dir{% else %}short-dir{% endif %}">
        {{ open_trade.action }}
        {% if open_trade.tp1_hit %}<span class="tp1-badge">TP1 ✓</span>{% endif %}
      </div>
      <div class="level-row"><span style="color:#6b6f8e;">Entry</span><span>{{ "%.2f"|format(open_trade.entry) }}</span></div>
      <div class="level-row"><span style="color:#e24b4a;">Stop Loss</span><span>{{ "%.2f"|format(open_trade.sl) }}</span></div>
      <div class="level-row"><span style="color:#f09820;">TP1 {% if open_trade.tp1_hit %}✓{% else %}(50%){% endif %}</span><span>{{ "%.2f"|format(open_trade.tp1) }}</span></div>
      <div class="level-row"><span style="color:#1dce8a;">TP2 (50%)</span><span>{{ "%.2f"|format(open_trade.tp2) }}</span></div>
      <div style="margin-top:12px;padding-top:10px;border-top:1px solid #1e2140;">
        <div class="level-row">
          <span style="color:#6b6f8e;">Unrealized</span>
          <span class="{% if unrealized>=0 %}pos{% else %}neg{% endif %}" style="font-size:17px;font-weight:700;">
            {% if unrealized>=0 %}+{% endif %}${{ "%.0f"|format(unrealized) }}
          </span>
        </div>
        {% if open_trade.tp1_hit %}
        <div class="level-row" style="font-size:12px;">
          <span style="color:#3a3e55;">TP1 locked in</span>
          <span style="color:#60b0f0;">+${{ "%.0f"|format(open_trade.tp1_pnl) }}</span>
        </div>
        {% endif %}
      </div>
      <div style="margin-top:14px;">
        <form action="/close" method="post" onsubmit="return confirm('Close trade at market?')">
          <button type="submit" style="background:#3d1212;color:#e24b4a;border:1px solid #6b2222;padding:7px 14px;border-radius:5px;cursor:pointer;font-size:12px;">Emergency Close</button>
        </form>
      </div>
    {% else %}
      <div class="flat-msg">— Flat —</div>
      <div style="font-size:12px;color:#3a3e55;margin-top:8px;">Waiting for next signal</div>
    {% endif %}
  </div>

  <div class="card" style="min-width:160px;">
    <h2>XAU/USD</h2>
    {% if last_price %}
      <div class="price-big">{{ "%.2f"|format(last_price.mid) }}</div>
      <div style="font-size:12px;color:#6b6f8e;">Bid {{ "%.2f"|format(last_price.bid) }} | Ask {{ "%.2f"|format(last_price.ask) }}</div>
    {% else %}
      <div style="color:#3a3e55;margin-top:8px;">Connecting to OANDA...</div>
      <div style="font-size:11px;color:#2a2e45;margin-top:6px;">Check OANDA_TOKEN env var</div>
    {% endif %}
  </div>

</div>

<div class="history">
  <h3>Trade History (last 100)</h3>
  {% if trades %}
  <table>
    <thead><tr>
      <th>#</th><th>Dir</th><th>Entry</th><th>SL</th><th>TP1</th><th>TP2</th>
      <th>Score</th><th>Status</th><th>P&L</th><th>Opened</th>
    </tr></thead>
    <tbody>
    {% for t in trades %}
    <tr>
      <td style="color:#3a3e55;">{{ t.id }}</td>
      <td class="{% if t.action=='LONG' %}long-dir{% else %}short-dir{% endif %}" style="font-weight:700;">{{ t.action }}</td>
      <td>{{ "%.2f"|format(t.entry) }}</td>
      <td style="color:#e24b4a;">{{ "%.2f"|format(t.sl) }}</td>
      <td style="color:#f09820;">{{ "%.2f"|format(t.tp1) }}</td>
      <td style="color:#1dce8a;">{{ "%.2f"|format(t.tp2) }}</td>
      <td style="color:#6b6f8e;">{{ t.score }}</td>
      <td><span class="badge badge-{{ t.status }}">{{ t.status }}</span></td>
      <td class="{% if t.pnl>0 %}pos{% elif t.pnl<0 %}neg{% else %}neu{% endif %}" style="font-weight:700;">
        {% if t.status=='OPEN' %}—{% elif t.pnl>=0 %}+${{ "%.0f"|format(t.pnl) }}{% else %}-${{ "%.0f"|format(t.pnl|abs) }}{% endif %}
      </td>
      <td style="color:#4a4e6a;font-size:12px;">{{ t.opened_at[:16] if t.opened_at else '—' }}</td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
    <div style="color:#3a3e55;padding:30px 0;">No trades yet. Waiting for TradingView signals.</div>
  {% endif %}
</div>
</body>
</html>
"""


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
                sl         REAL    NOT NULL,
                tp1        REAL    NOT NULL,
                tp2        REAL    NOT NULL,
                score      TEXT    DEFAULT '',
                tp1_hit    INTEGER DEFAULT 0,
                tp1_pnl    REAL    DEFAULT 0,
                status     TEXT    DEFAULT 'OPEN',
                pnl        REAL    DEFAULT 0,
                opened_at  TEXT,
                closed_at  TEXT
            )
        """)
        conn.commit()

def calc_pnl(action, entry, exit_price, oz):
    if action == "LONG":
        return (exit_price - entry) * oz
    return (entry - exit_price) * oz

def price_monitor():
    global open_trade, last_price
    if not OANDA_OK or not OANDA_TOKEN:
        print("[Monitor] OANDA not configured — price monitoring disabled.")
        print("[Monitor] Set OANDA_TOKEN and ACCOUNT_ID environment variables.")
        return
    client = oandapyV20.API(access_token=OANDA_TOKEN, environment="practice")
    while True:
        try:
            r = pricing.PricingStream(accountID=ACCOUNT_ID, params={"instruments": INSTRUMENT})
            for tick in client.request(r):
                if tick.get("type") != "PRICE":
                    continue
                bid = float(tick["bids"][0]["price"])
                ask = float(tick["asks"][0]["price"])
                mid = (bid + ask) / 2.0
                with lock:
                    last_price = {"bid": bid, "ask": ask, "mid": mid}
                    if open_trade is None:
                        continue
                    t      = open_trade
                    action = t["action"]
                    if action == "LONG":
                        sl_hit  = bid <= t["sl"]
                        tp1_hit = (not t["tp1_hit"]) and ask >= t["tp1"]
                        tp2_hit = t["tp1_hit"] and ask >= t["tp2"]
                    else:
                        sl_hit  = ask >= t["sl"]
                        tp1_hit = (not t["tp1_hit"]) and bid <= t["tp1"]
                        tp2_hit = t["tp1_hit"] and bid <= t["tp2"]
                    now_str = datetime.now(timezone.utc).isoformat()
                    if tp2_hit:
                        tp2_pnl   = calc_pnl(action, t["entry"], t["tp2"], OZ_HALF)
                        total_pnl = round(t["tp1_pnl"] + tp2_pnl, 2)
                        with get_db() as conn:
                            conn.execute("UPDATE trades SET status='WIN', pnl=?, closed_at=? WHERE id=?",
                                (total_pnl, now_str, t["id"]))
                            conn.commit()
                        print(f"[Trade #{t['id']}] WIN  +${total_pnl:.2f}")
                        open_trade = None
                    elif tp1_hit:
                        tp1_pnl = calc_pnl(action, t["entry"], t["tp1"], OZ_HALF)
                        open_trade["tp1_hit"] = True
                        open_trade["tp1_pnl"] = tp1_pnl
                        with get_db() as conn:
                            conn.execute("UPDATE trades SET tp1_hit=1, tp1_pnl=? WHERE id=?",
                                (round(tp1_pnl, 2), t["id"]))
                            conn.commit()
                        print(f"[Trade #{t['id']}] TP1 hit  ${tp1_pnl:.2f}")
                    elif sl_hit:
                        if t["tp1_hit"]:
                            sl_pnl    = calc_pnl(action, t["entry"], t["sl"], OZ_HALF)
                            total_pnl = round(t["tp1_pnl"] + sl_pnl, 2)
                            status    = "PARTIAL"
                        else:
                            total_pnl = round(calc_pnl(action, t["entry"], t["sl"], OZ_FULL), 2)
                            status    = "LOSS"
                        with get_db() as conn:
                            conn.execute("UPDATE trades SET status=?, pnl=?, closed_at=? WHERE id=?",
                                (status, total_pnl, now_str, t["id"]))
                            conn.commit()
                        sign = "+" if total_pnl >= 0 else ""
                        print(f"[Trade #{t['id']}] {status}  {sign}${total_pnl:.2f}")
                        open_trade = None
        except Exception as e:
            print(f"[Monitor] Error: {e} — reconnecting in 5s")
            time.sleep(5)

@app.route("/webhook", methods=["POST"])
def webhook():
    global open_trade
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "no JSON received"}), 400
        action = str(data.get("action", "")).upper()
        if action not in ("LONG", "SHORT"):
            return jsonify({"error": f"invalid action: {action}"}), 400
        entry = float(data["entry"])
        sl    = float(data["sl"])
        tp1   = float(data["tp1"])
        tp2   = float(data["tp2"])
        score = str(data.get("score", ""))
        with lock:
            if open_trade is not None:
                return jsonify({"status": "skipped", "reason": "already in trade", "open_id": open_trade["id"]}), 200
            now_str = datetime.now(timezone.utc).isoformat()
            with get_db() as conn:
                cur = conn.execute(
                    "INSERT INTO trades (action,entry,sl,tp1,tp2,score,status,opened_at) VALUES (?,?,?,?,?,?,'OPEN',?)",
                    (action, entry, sl, tp1, tp2, score, now_str))
                trade_id = cur.lastrowid
                conn.commit()
            open_trade = {"id": trade_id, "action": action, "entry": entry,
                          "sl": sl, "tp1": tp1, "tp2": tp2, "tp1_hit": False, "tp1_pnl": 0.0}
        print(f"[Trade #{trade_id}] Opened {action} @ {entry}  SL:{sl}  TP1:{tp1}  TP2:{tp2}")
        return jsonify({"status": "ok", "trade_id": trade_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/close", methods=["POST"])
def manual_close():
    global open_trade
    with lock:
        if open_trade is None:
            return jsonify({"status": "no open trade"}), 200
        price = last_price["mid"] if last_price else open_trade["entry"]
        if open_trade["tp1_hit"]:
            pnl = round(open_trade["tp1_pnl"] + calc_pnl(open_trade["action"], open_trade["entry"], price, OZ_HALF), 2)
        else:
            pnl = round(calc_pnl(open_trade["action"], open_trade["entry"], price, OZ_FULL), 2)
        now_str = datetime.now(timezone.utc).isoformat()
        with get_db() as conn:
            conn.execute("UPDATE trades SET status='MANUAL', pnl=?, closed_at=? WHERE id=?",
                (pnl, now_str, open_trade["id"]))
            conn.commit()
        trade_id   = open_trade["id"]
        open_trade = None
    return jsonify({"status": "closed", "trade_id": trade_id, "pnl": pnl})

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
    partial = sum(1 for t in closed if t["status"] == "PARTIAL")
    count   = len(closed)
    wr      = round(wins / count * 100) if count > 0 else 0
    best    = max((t["pnl"] for t in closed), default=0)
    worst   = min((t["pnl"] for t in closed), default=0)
    with lock:
        cur_trade = open_trade
        cur_price = last_price
    unrealized = 0.0
    if cur_trade and cur_price:
        mid = cur_price["mid"]
        if cur_trade["tp1_hit"]:
            unrealized = cur_trade["tp1_pnl"] + calc_pnl(cur_trade["action"], cur_trade["entry"], mid, OZ_HALF)
        else:
            unrealized = calc_pnl(cur_trade["action"], cur_trade["entry"], mid, OZ_FULL)
    return render_template_string(DASHBOARD_HTML,
        month=now.strftime("%b %Y"), total=total, wins=wins, losses=losses,
        partial=partial, count=count, win_rate=wr, best=best, worst=worst,
        open_trade=cur_trade, unrealized=unrealized, last_price=cur_price,
        trades=all_t, lot_size=LOT_SIZE)

@app.route("/status")
def status():
    with lock:
        return jsonify({"open_trade": open_trade, "last_price": last_price})

init_db()
monitor_thread = threading.Thread(target=price_monitor, daemon=True)
monitor_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
