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
<title>Apex Paper Bot</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{margin:0;padding:0;box-sizing:border-box}
body{background:#080b14;color:#9ba4c8;font-family:Inter,-apple-system,sans-serif;font-size:13px;line-height:1.5}
.mono{font-family:"JetBrains Mono",monospace}
.hd{background:#0b0e1e;border-bottom:1px solid #181c38;padding:12px 24px;display:flex;align-items:center;justify-content:space-between}
.hd-name{font-size:15px;font-weight:500;color:#e2e6f8;letter-spacing:-0.3px}
.hd-r{display:flex;align-items:center;gap:10px;font-size:11px;color:#2e3358}
.pill{display:flex;align-items:center;gap:5px;border-radius:20px;padding:3px 10px;font-size:11px}
.pill-on{background:#051a10;border:1px solid #0a3820;color:#00d17a}
.pill-off{background:#1a0208;border:1px solid #3d0815;color:#ff4060}
.dot{width:6px;height:6px;border-radius:50%}
.dot-on{background:#00d17a}
.dot-off{background:#ff4060}
@keyframes blink{0%,100%{opacity:1}50%{opacity:0.35}}
.blink{animation:blink 2s ease infinite}
.tags{padding:7px 24px;border-bottom:1px solid #0e1228;display:flex;gap:6px;flex-wrap:wrap;background:#070a18}
.tag{background:#0c0f26;border:1px solid #181c3a;border-radius:5px;padding:2px 8px;font-size:11px;color:#3d4468}
.grid{display:grid;grid-template-columns:210px 1fr 180px;gap:14px;padding:18px 24px}
.card{background:#0d1021;border:1px solid #171c35;border-radius:10px;padding:16px 18px}
.card-lbl{font-size:9px;text-transform:uppercase;letter-spacing:1.2px;color:#252948;margin-bottom:14px;font-weight:500}
.pnl-num{font-family:"JetBrains Mono",monospace;font-size:30px;font-weight:400;line-height:1;margin-bottom:14px}
.g{color:#00d17a}.r{color:#ff4060}.b{color:#4a9eff}.mu{color:#2e3358}
.sr{display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid #0a0d20}
.sr:last-child{border:none}
.sk{font-size:11px;color:#3d4468}.sv{font-family:"JetBrains Mono",monospace;font-size:11px;color:#9ba4c8}
.dir-b{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:5px;font-size:12px;font-weight:500;letter-spacing:0.3px;margin-bottom:14px}
.dl{background:#021a0e;border:1px solid #083d20;color:#00d17a}
.ds{background:#1a0208;border:1px solid #3d0815;color:#ff4060}
.lr{display:flex;justify-content:space-between;align-items:center;padding:4px 0}
.lk{font-size:11px;color:#3d4468}.lv{font-family:"JetBrains Mono",monospace;font-size:12px}
.rr-wrap{margin:9px 0 7px;background:#0a0d1e;border-radius:4px;height:5px;position:relative;overflow:hidden}
.rr-sl{position:absolute;left:0;top:0;height:100%;background:#ff4060;border-radius:3px 0 0 3px}
.rr-tp{position:absolute;right:0;top:0;height:100%;background:#00d17a;border-radius:0 3px 3px 0}
.ur{margin-top:12px;padding-top:12px;border-top:1px solid #171c35}
.ur-lbl{font-size:9px;text-transform:uppercase;letter-spacing:0.8px;color:#252948;margin-bottom:3px}
.ur-val{font-family:"JetBrains Mono",monospace;font-size:20px}
.close-btn{display:block;width:100%;margin-top:12px;padding:6px;background:transparent;border:1px solid #3d0815;border-radius:6px;color:#ff4060;font-size:11px;cursor:pointer;letter-spacing:0.2px}
.close-btn:hover{background:#1a0208}
.flat-txt{color:#252948;font-size:13px;margin-top:6px}
.flat-txt small{display:block;margin-top:4px;font-size:11px;color:#181c35}
.px-main{font-family:"JetBrains Mono",monospace;font-size:26px;color:#e2e6f8;letter-spacing:-1px;margin:8px 0 5px;line-height:1}
.px-ba{font-size:11px;color:#252948;line-height:2}
.px-st{display:flex;align-items:center;gap:5px;font-size:10px;margin-bottom:2px}
.nc{font-size:11px;color:#252948;line-height:2}
.hist{padding:0 24px 28px}
.hist-h{font-size:9px;text-transform:uppercase;letter-spacing:1.2px;color:#252948;margin-bottom:10px;font-weight:500}
table{width:100%;border-collapse:collapse}
th{font-size:9px;text-transform:uppercase;letter-spacing:0.8px;color:#252948;padding:7px 10px;text-align:left;border-bottom:1px solid #0d1021;font-weight:500}
td{padding:8px 10px;border-bottom:1px solid #09091e;font-size:12px}
tr:hover td{background:#0b0e1e}
.badge{display:inline-block;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:500;text-transform:uppercase;letter-spacing:0.3px}
.bW{background:#021a0e;color:#00d17a;border:1px solid #083d20}
.bL{background:#1a0208;color:#ff4060;border:1px solid #3d0815}
.bO{background:#021020;color:#4a9eff;border:1px solid #0a2850}
.bM{background:#0c0f26;color:#3d4468;border:1px solid #181c3a}
.nt{color:#1e2248;padding:20px 0;font-size:12px}
</style>
</head>
<body>

<div class="hd">
  <div style="display:flex;align-items:center;gap:12px">
    <span class="hd-name">Apex Paper Bot</span>
    {% if last_price %}
      <div class="pill pill-on"><span class="dot dot-on blink"></span>Live</div>
    {% else %}
      <div class="pill pill-off"><span class="dot dot-off"></span>Offline</div>
    {% endif %}
  </div>
  <div class="hd-r">
    <span>{{ month }}</span>
    <span style="color:#141830">|</span>
    <span>auto-refresh 10s</span>
  </div>
</div>

<div class="tags">
  <span class="tag">100% exit @ TP1</span>
  <span class="tag">{{ slippage }}pt slippage</span>
  <span class="tag">min {{ min_rr }}:1 RR</span>
  <span class="tag">{{ lot_size }} lot = ${{ (lot_size*100)|int }}/pt</span>
</div>

<div class="grid">

  <div class="card">
    <div class="card-lbl">{{ month }} performance</div>
    {% if total >= 0 %}
      <div class="pnl-num g">+${{ "%.0f"|format(total) }}</div>
    {% else %}
      <div class="pnl-num r">-${{ "%.0f"|format(total|abs) }}</div>
    {% endif %}
    <div class="sr"><span class="sk">Trades</span><span class="sv">{{ count }}</span></div>
    <div class="sr"><span class="sk">Wins</span><span class="sv g">{{ wins }}</span></div>
    <div class="sr"><span class="sk">Losses</span><span class="sv r">{{ losses }}</span></div>
    <div class="sr"><span class="sk">Win rate</span><span class="sv">{{ win_rate }}%</span></div>
    <div class="sr"><span class="sk">Best trade</span><span class="sv g">{% if best > 0 %}+${{ "%.0f"|format(best) }}{% else %}—{% endif %}</span></div>
    <div class="sr"><span class="sk">Worst trade</span><span class="sv r">{% if worst < 0 %}-${{ "%.0f"|format(worst|abs) }}{% else %}—{% endif %}</span></div>
    <div class="sr"><span class="sk">Skipped (RR)</span><span class="sv mu">{{ skipped }}</span></div>
  </div>

  <div class="card">
    <div class="card-lbl">open trade</div>
    {% if open_trade %}
      <div class="dir-b {% if open_trade.action=='LONG' %}dl{% else %}ds{% endif %}">
        {{ open_trade.action }}
        <span style="font-size:10px;opacity:0.55;font-weight:400">{{ "%.2f"|format(open_trade.rr) }}R</span>
      </div>
      <div class="lr"><span class="lk">Entry</span><span class="lv">{{ "%.2f"|format(open_trade.entry) }}</span></div>
      <div class="lr"><span class="lk r">Stop loss</span><span class="lv r">{{ "%.2f"|format(open_trade.sl) }}</span></div>
      {% set trng = open_trade.rr + 1 %}
      {% set slp = (1 / trng * 100)|round(1) %}
      {% set tpp = (open_trade.rr / trng * 100)|round(1) %}
      <div class="rr-wrap">
        <div class="rr-sl" style="width:{{ slp }}%"></div>
        <div class="rr-tp" style="width:{{ tpp }}%"></div>
      </div>
      <div class="lr"><span class="lk g">TP1 — 100% exit</span><span class="lv g">{{ "%.2f"|format(open_trade.tp1) }}</span></div>
      <div class="ur">
        <div class="ur-lbl">Unrealized P&amp;L</div>
        <div class="ur-val {% if unrealized>=0 %}g{% else %}r{% endif %}">{% if unrealized>=0 %}+{% endif %}${{ "%.0f"|format(unrealized) }}</div>
      </div>
      <form action="/close" method="post" onsubmit="return confirm('Close at market?')">
        <button type="submit" class="close-btn">Emergency close</button>
      </form>
    {% else %}
      <div class="flat-txt">— Flat —<small>Waiting for next TradingView signal</small></div>
    {% endif %}
  </div>

  <div class="card">
    <div class="card-lbl">XAU / USD</div>
    {% if last_price %}
      <div class="px-st"><span class="dot dot-on blink"></span><span class="g" style="font-size:10px">OANDA connected</span></div>
      <div class="px-main">{{ "%.2f"|format(last_price.mid) }}</div>
      <div class="px-ba">Bid {{ "%.2f"|format(last_price.bid) }}<br>Ask {{ "%.2f"|format(last_price.ask) }}</div>
    {% else %}
      <div class="px-st"><span class="dot dot-off"></span><span class="r" style="font-size:10px">Not connected</span></div>
      <div class="nc">Railway → Variables<br>Add OANDA_TOKEN<br>Add ACCOUNT_ID<br>Then redeploy</div>
    {% endif %}
  </div>

</div>

<div class="hist">
  <div class="hist-h">trade history — last 100</div>
  {% if trades %}
  <table>
    <thead><tr>
      <th>#</th><th>Dir</th><th>Entry</th>
      <th>SL raw → adj</th><th>TP1 raw → adj</th>
      <th>RR</th><th>Score</th><th>Status</th><th>P&amp;L</th><th>Opened</th>
    </tr></thead>
    <tbody>
    {% for t in trades %}
    <tr>
      <td class="mu">{{ t.id }}</td>
      <td class="{% if t.action=='LONG' %}g{% else %}r{% endif %}" style="font-weight:500">{{ t.action }}</td>
      <td class="mono">{{ "%.2f"|format(t.entry) }}</td>
      <td class="mono r">{{ "%.2f"|format(t.sl_raw) }} → {{ "%.2f"|format(t.sl) }}</td>
      <td class="mono g">{{ "%.2f"|format(t.tp1_raw) }} → {{ "%.2f"|format(t.tp1) }}</td>
      <td class="mono mu">{{ "%.2f"|format(t.rr) }}</td>
      <td class="mu">{{ t.score }}</td>
      <td>
        {% if t.status=='WIN' %}<span class="badge bW">WIN</span>
        {% elif t.status=='LOSS' %}<span class="badge bL">LOSS</span>
        {% elif t.status=='OPEN' %}<span class="badge bO">OPEN</span>
        {% else %}<span class="badge bM">{{ t.status }}</span>{% endif %}
      </td>
      <td class="mono {% if t.pnl>0 %}g{% elif t.pnl<0 %}r{% else %}mu{% endif %}">
        {% if t.status=='OPEN' %}—{% elif t.pnl>=0 %}+${{ "%.0f"|format(t.pnl) }}{% else %}-${{ "%.0f"|format(t.pnl|abs) }}{% endif %}
      </td>
      <td class="mu" style="font-size:11px">{{ t.opened_at[:16] if t.opened_at else '—' }}</td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
  <div class="nt">No trades yet — waiting for TradingView signals</div>
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
