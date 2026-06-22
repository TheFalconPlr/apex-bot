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

# ── Config ─────────────────────────────────────────────────────────────
OANDA_TOKEN = os.environ.get("OANDA_TOKEN", "")
ACCOUNT_ID  = os.environ.get("ACCOUNT_ID",  "")
INSTRUMENT  = "XAU_USD"
LOT_SIZE    = float(os.environ.get("LOT_SIZE",  "0.5"))
SLIPPAGE    = float(os.environ.get("SLIPPAGE",  "1.2"))   # points adjustment on TP + SL
MIN_RR      = float(os.environ.get("MIN_RR",    "1.0"))   # minimum risk:reward to take trade
OZ_FULL     = LOT_SIZE * 100                               # 0.5 lot = 50 oz
DB_PATH     = "trades.db"

lock       = threading.Lock()
open_trade = None
last_price = None

# ── Slippage logic ─────────────────────────────────────────────────────
# LONG:  TP adjusted DOWN 1.2pt (harder to reach), SL adjusted DOWN 1.2pt (easier to hit)
# SHORT: TP adjusted UP   1.2pt (harder to reach), SL adjusted UP   1.2pt (easier to hit)
def adjust_levels(action, tp1, sl):
    if action == "LONG":
        return tp1 - SLIPPAGE, sl - SLIPPAGE
    else:
        return tp1 + SLIPPAGE, sl + SLIPPAGE

# ── RR check ───────────────────────────────────────────────────────────
def calc_rr(action, entry, adj_tp1, adj_sl):
    if action == "LONG":
        reward = adj_tp1 - entry
        risk   = entry   - adj_sl
    else:
        reward = entry   - adj_tp1
        risk   = adj_sl  - entry
    if risk <= 0:
        return 0.0
    return reward / risk

# ── PnL helper ─────────────────────────────────────────────────────────
def calc_pnl(action, entry, exit_price, oz):
    if action == "LONG":
        return (exit_price - entry) * oz
    return (entry - exit_price) * oz

# ── Database ───────────────────────────────────────────────────────────
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

# ── Price monitor ──────────────────────────────────────────────────────
def price_monitor():
    global open_trade, last_price
    if not OANDA_OK or not OANDA_TOKEN:
        print("[Monitor] OANDA not configured — add OANDA_TOKEN + ACCOUNT_ID env vars.")
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
                    # For LONG:  TP hit when ask >= tp1, SL hit when bid <= sl
                    # For SHORT: TP hit when bid <= tp1, SL hit when ask >= sl
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
                            conn.execute("UPDATE trades SET status='WIN', pnl=?, closed_at=? WHERE id=?",
                                (pnl, now_str, t["id"]))
                            conn.commit()
                        print(f"[Trade #{t['id']}] WIN  +${pnl:.2f}")
                        open_trade = None
                    elif sl_hit:
                        pnl = round(calc_pnl(action, t["entry"], t["sl"], OZ_FULL), 2)
                        with get_db() as conn:
                            conn.execute("UPDATE trades SET status='LOSS', pnl=?, closed_at=? WHERE id=?",
                                (pnl, now_str, t["id"]))
                            conn.commit()
                        print(f"[Trade #{t['id']}] LOSS  ${pnl:.2f}")
                        open_trade = None
        except Exception as e:
            print(f"[Monitor] Error: {e} — reconnecting in 5s")
            time.sleep(5)

# ── Webhook ────────────────────────────────────────────────────────────
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
        # Apply slippage to get realistic levels
        adj_tp1, adj_sl = adjust_levels(action, tp1_raw, sl_raw)
        # RR filter
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
        print(f"[Trade #{trade_id}] {action} @ {entry}  SL:{adj_sl} (+{SLIPPAGE}pt adj)  TP1:{adj_tp1} (-{SLIPPAGE}pt adj)  RR:{rr:.2f}")
        return jsonify({"status": "ok", "trade_id": trade_id, "rr": round(rr, 2)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Manual close ───────────────────────────────────────────────────────
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

# ── Dashboard ──────────────────────────────────────────────────────────
DASHBOARD = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta http-equiv="refresh" content="15">
<title>Apex Paper Bot — {{ month }}</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:#0b0d17;color:#c8cad8;font-family:'Segoe UI',sans-serif;font-size:14px}
  .top{display:flex;gap:16px;padding:20px;flex-wrap:wrap}
  .card{background:#13162a;border:1px solid #222640;border-radius:10px;padding:18px 22px}
  .card h2{font-size:11px;color:#6b6f8e;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px}
  .pnl-big{font-size:36px;font-weight:700;margin-bottom:14px}
  .pos{color:#1dce8a}.neg{color:#e24b4a}.neu{color:#8890b0}
  .stat-row{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #1a1d33}
  .stat-row:last-child{border-bottom:none}
  .stat-label{color:#6b6f8e}.stat-val{font-weight:600}
  .level-row{display:flex;justify-content:space-between;padding:4px 0;font-size:13px}
  .long-dir{color:#1dce8a}.short-dir{color:#e24b4a}
  .history{padding:0 20px 30px}
  .history h3{font-size:12px;color:#6b6f8e;text-transform:uppercase;letter-spacing:1px;margin-bottom:12px}
  table{width:100%;border-collapse:collapse}
  th{font-size:11px;color:#6b6f8e;text-transform:uppercase;padding:8px 12px;text-align:left;background:#0f1121;border-bottom:1px solid #1e2140}
  td{padding:9px 12px;border-bottom:1px solid #141728;font-size:13px}
  tr:hover td{background:#141930}
  .badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;text-transform:uppercase}
  .badge-WIN{background:#0d3d2a;color:#1dce8a}
  .badge-LOSS{background:#3d1212;color:#e24b4a}
  .badge-OPEN{background:#1a2060;color:#7090ff}
  .badge-MANUAL{background:#2a2a3a;color:#8890b0}
  .info-bar{font-size:11px;color:#3a3e55;padding:6px 20px}
  .tag{display:inline-block;background:#1a1d33;border:1px solid #2a2d45;border-radius:4px;padding:2px 8px;font-size:11px;color:#6b6f8e;margin-right:6px}
</style>
</head>
<body>
<div style="padding:20px 20px 0;display:flex;align-items:center;gap:12px;">
  <div style="font-size:20px;font-weight:700;color:#e8e9f0;">Apex Paper Bot</div>
  <div style="font-size:13px;color:#6b6f8e;">{{ month }} — paper trading only</div>
</div>
<div class="info-bar">
  <span class="tag">100% exit @ TP1</span>
  <span class="tag">{{ slippage }}pt slippage on SL + TP</span>
  <span class="tag">min {{ min_rr }}:1 RR</span>
  <span class="tag">{{ lot_size }} lot = ${{ (lot_size * 100)|int }}/pt</span>
  <span style="color:#2a2e45;">Auto-refreshes every 15s</span>
</div>

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
    <div class="stat-row"><span class="stat-label">Losses</span><span class="stat-val neg">{{ losses }}</span></div>
    <div class="stat-row"><span class="stat-label">Win rate</span><span class="stat-val">{{ win_rate }}%</span></div>
    <div class="stat-row"><span class="stat-label">Best trade</span><span class="stat-val pos">{% if best > 0 %}+${{ "%.0f"|format(best) }}{% else %}—{% endif %}</span></div>
    <div class="stat-row"><span class="stat-label">Worst trade</span><span class="stat-val neg">{% if worst < 0 %}-${{ "%.0f"|format(worst|abs) }}{% else %}—{% endif %}</span></div>
    <div class="stat-row"><span class="stat-label">Skipped (RR)</span><span class="stat-val neu">{{ skipped }}</span></div>
  </div>

  <div class="card" style="min-width:270px;">
    <h2>Open Trade</h2>
    {% if open_trade %}
      <div style="font-size:22px;font-weight:700;margin-bottom:12px;" class="{% if open_trade.action=='LONG' %}long-dir{% else %}short-dir{% endif %}">
        {{ open_trade.action }} &nbsp;<span style="font-size:13px;color:#6b6f8e;font-weight:400;">RR {{ "%.2f"|format(open_trade.rr) }}</span>
      </div>
      <div class="level-row"><span style="color:#6b6f8e;">Entry</span><span>{{ "%.2f"|format(open_trade.entry) }}</span></div>
      <div class="level-row"><span style="color:#e24b4a;">Stop Loss <span style="font-size:11px;color:#4a4e6a;">(adj)</span></span><span>{{ "%.2f"|format(open_trade.sl) }}</span></div>
      <div class="level-row"><span style="color:#1dce8a;">TP1 — 100% exit <span style="font-size:11px;color:#4a4e6a;">(adj)</span></span><span>{{ "%.2f"|format(open_trade.tp1) }}</span></div>
      <div style="margin-top:12px;padding-top:10px;border-top:1px solid #1e2140;">
        <div class="level-row">
          <span style="color:#6b6f8e;">Unrealized</span>
          <span class="{% if unrealized>=0 %}pos{% else %}neg{% endif %}" style="font-size:17px;font-weight:700;">
            {% if unrealized>=0 %}+{% endif %}${{ "%.0f"|format(unrealized) }}
          </span>
        </div>
      </div>
      <div style="margin-top:14px;">
        <form action="/close" method="post" onsubmit="return confirm('Close at market price?')">
          <button type="submit" style="background:#3d1212;color:#e24b4a;border:1px solid #6b2222;padding:7px 14px;border-radius:5px;cursor:pointer;font-size:12px;">Emergency Close</button>
        </form>
      </div>
    {% else %}
      <div style="color:#4a4e6a;font-size:16px;margin-top:10px;">— Flat —</div>
      <div style="font-size:12px;color:#3a3e55;margin-top:8px;">Waiting for next signal</div>
    {% endif %}
  </div>

  <div class="card" style="min-width:160px;">
    <h2>XAU/USD</h2>
    {% if last_price %}
      <div style="font-size:26px;font-weight:700;color:#e8e9f0;margin-bottom:6px;">{{ "%.2f"|format(last_price.mid) }}</div>
      <div style="font-size:12px;color:#6b6f8e;">Bid {{ "%.2f"|format(last_price.bid) }} | Ask {{ "%.2f"|format(last_price.ask) }}</div>
    {% else %}
      <div style="color:#3a3e55;margin-top:8px;">Connecting to OANDA...</div>
      <div style="font-size:11px;color:#2a2e45;margin-top:6px;">Add OANDA_TOKEN + ACCOUNT_ID in Railway → Variables</div>
    {% endif %}
  </div>

</div>

<div class="history">
  <h3>Trade History (last 100)</h3>
  {% if trades %}
  <table>
    <thead><tr>
      <th>#</th><th>Dir</th><th>Entry</th>
      <th>SL (raw → adj)</th><th>TP1 (raw → adj)</th>
      <th>RR</th><th>Score</th><th>Status</th><th>P&L</th><th>Opened</th>
    </tr></thead>
    <tbody>
    {% for t in trades %}
    <tr>
      <td style="color:#3a3e55;">{{ t.id }}</td>
      <td class="{% if t.action=='LONG' %}long-dir{% else %}short-dir{% endif %}" style="font-weight:700;">{{ t.action }}</td>
      <td>{{ "%.2f"|format(t.entry) }}</td>
      <td style="color:#e24b4a;">{{ "%.2f"|format(t.sl_raw) }} → {{ "%.2f"|format(t.sl) }}</td>
      <td style="color:#1dce8a;">{{ "%.2f"|format(t.tp1_raw) }} → {{ "%.2f"|format(t.tp1) }}</td>
      <td style="color:#8890b0;">{{ "%.2f"|format(t.rr) }}</td>
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
        return jsonify({"open_trade": open_trade, "last_price": last_price,
                        "config": {"slippage": SLIPPAGE, "min_rr": MIN_RR, "lot_size": LOT_SIZE}})

init_db()
monitor_thread = threading.Thread(target=price_monitor, daemon=True)
monitor_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
