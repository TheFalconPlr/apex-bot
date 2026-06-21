import os
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template

try:
    import oandapyV20
    import oandapyV20.endpoints.pricing as pricing
    OANDA_OK = True
except ImportError:
    OANDA_OK = False

app = Flask(__name__)

# ── Config (set these as environment variables on Railway) ─────────────
OANDA_TOKEN = os.environ.get("OANDA_TOKEN", "")
ACCOUNT_ID  = os.environ.get("ACCOUNT_ID",  "")
INSTRUMENT  = "XAU_USD"
LOT_SIZE    = float(os.environ.get("LOT_SIZE", "0.5"))
OZ_FULL     = LOT_SIZE * 100        # e.g. 0.5 lot = 50 oz
OZ_HALF     = OZ_FULL * 0.5         # 25 oz per half
DB_PATH     = "trades.db"

# ── Shared state (thread-safe) ─────────────────────────────────────────
lock        = threading.Lock()
open_trade  = None   # dict while in trade, None when flat
last_price  = None   # {"bid": x, "ask": x, "mid": x}


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


# ── PnL helper ─────────────────────────────────────────────────────────
def calc_pnl(action, entry, exit_price, oz):
    if action == "LONG":
        return (exit_price - entry) * oz
    return (entry - exit_price) * oz


# ── Price monitor (background thread) ─────────────────────────────────
def price_monitor():
    global open_trade, last_price

    if not OANDA_OK or not OANDA_TOKEN:
        print("[Monitor] OANDA not configured — price monitoring disabled.")
        print("[Monitor] Set OANDA_TOKEN and ACCOUNT_ID environment variables.")
        return

    client = oandapyV20.API(access_token=OANDA_TOKEN, environment="practice")

    while True:
        try:
            r = pricing.PricingStream(
                accountID=ACCOUNT_ID,
                params={"instruments": INSTRUMENT}
            )
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
                    else:  # SHORT
                        sl_hit  = ask >= t["sl"]
                        tp1_hit = (not t["tp1_hit"]) and bid <= t["tp1"]
                        tp2_hit = t["tp1_hit"] and bid <= t["tp2"]

                    now_str = datetime.now(timezone.utc).isoformat()

                    if tp2_hit:
                        # Both TP1 and TP2 hit → WIN
                        tp2_pnl   = calc_pnl(action, t["entry"], t["tp2"], OZ_HALF)
                        total_pnl = round(t["tp1_pnl"] + tp2_pnl, 2)
                        with get_db() as conn:
                            conn.execute(
                                "UPDATE trades SET status='WIN', pnl=?, closed_at=? WHERE id=?",
                                (total_pnl, now_str, t["id"])
                            )
                            conn.commit()
                        print(f"[Trade #{t['id']}] WIN  +${total_pnl:.2f}")
                        open_trade = None

                    elif tp1_hit:
                        # TP1 hit → book first half, keep second half running
                        tp1_pnl = calc_pnl(action, t["entry"], t["tp1"], OZ_HALF)
                        open_trade["tp1_hit"] = True
                        open_trade["tp1_pnl"] = tp1_pnl
                        with get_db() as conn:
                            conn.execute(
                                "UPDATE trades SET tp1_hit=1, tp1_pnl=? WHERE id=?",
                                (round(tp1_pnl, 2), t["id"])
                            )
                            conn.commit()
                        print(f"[Trade #{t['id']}] TP1 hit  ${tp1_pnl:.2f} — watching TP2/SL")

                    elif sl_hit:
                        if t["tp1_hit"]:
                            # TP1 was hit, SL catches second half → PARTIAL
                            sl_pnl    = calc_pnl(action, t["entry"], t["sl"], OZ_HALF)
                            total_pnl = round(t["tp1_pnl"] + sl_pnl, 2)
                            status    = "PARTIAL"
                        else:
                            # SL before TP1 → full LOSS
                            total_pnl = round(calc_pnl(action, t["entry"], t["sl"], OZ_FULL), 2)
                            status    = "LOSS"
                        with get_db() as conn:
                            conn.execute(
                                "UPDATE trades SET status=?, pnl=?, closed_at=? WHERE id=?",
                                (status, total_pnl, now_str, t["id"])
                            )
                            conn.commit()
                        sign = "+" if total_pnl >= 0 else ""
                        print(f"[Trade #{t['id']}] {status}  {sign}${total_pnl:.2f}")
                        open_trade = None

        except Exception as e:
            print(f"[Monitor] Error: {e} — reconnecting in 5s")
            time.sleep(5)


# ── Webhook endpoint (TradingView sends here) ──────────────────────────
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
                return jsonify({
                    "status": "skipped",
                    "reason": "already in trade",
                    "open_id": open_trade["id"]
                }), 200

            now_str = datetime.now(timezone.utc).isoformat()
            with get_db() as conn:
                cur = conn.execute(
                    "INSERT INTO trades (action,entry,sl,tp1,tp2,score,status,opened_at) VALUES (?,?,?,?,?,?,'OPEN',?)",
                    (action, entry, sl, tp1, tp2, score, now_str)
                )
                trade_id = cur.lastrowid
                conn.commit()

            open_trade = {
                "id":      trade_id,
                "action":  action,
                "entry":   entry,
                "sl":      sl,
                "tp1":     tp1,
                "tp2":     tp2,
                "tp1_hit": False,
                "tp1_pnl": 0.0
            }

        print(f"[Trade #{trade_id}] Opened {action} @ {entry}  SL:{sl}  TP1:{tp1}  TP2:{tp2}  Score:{score}")
        return jsonify({"status": "ok", "trade_id": trade_id})

    except Exception as e:
        print(f"[Webhook] Error: {e}")
        return jsonify({"error": str(e)}), 500


# ── Manual close (emergency) ───────────────────────────────────────────
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
            conn.execute(
                "UPDATE trades SET status='MANUAL', pnl=?, closed_at=? WHERE id=?",
                (pnl, now_str, open_trade["id"])
            )
            conn.commit()

        trade_id = open_trade["id"]
        open_trade = None

    return jsonify({"status": "closed", "trade_id": trade_id, "pnl": pnl})


# ── Dashboard ──────────────────────────────────────────────────────────
@app.route("/")
def dashboard():
    now         = datetime.now(timezone.utc)
    month_start = f"{now.year}-{now.month:02d}-01"

    with get_db() as conn:
        month_rows = conn.execute(
            "SELECT * FROM trades WHERE status != 'OPEN' AND opened_at >= ? ORDER BY opened_at DESC",
            (month_start,)
        ).fetchall()
        all_rows = conn.execute(
            "SELECT * FROM trades ORDER BY opened_at DESC LIMIT 100"
        ).fetchall()

    closed = [dict(r) for r in month_rows]
    all_t  = [dict(r) for r in all_rows]

    total   = sum(t["pnl"] for t in closed)
    wins    = sum(1 for t in closed if t["status"] == "WIN")
    losses  = sum(1 for t in closed if t["status"] == "LOSS")
    partial = sum(1 for t in closed if t["status"] == "PARTIAL")
    count   = len(closed)
    wr      = round(wins / count * 100) if count > 0 else 0
    best    = max((t["pnl"] for t in closed), default=0)
    worst   = min((t["pnl"] for t in closed), default=0)

    with lock:
        cur_trade  = open_trade
        cur_price  = last_price

    unrealized = 0.0
    if cur_trade and cur_price:
        mid = cur_price["mid"]
        if cur_trade["tp1_hit"]:
            unrealized = cur_trade["tp1_pnl"] + calc_pnl(cur_trade["action"], cur_trade["entry"], mid, OZ_HALF)
        else:
            unrealized = calc_pnl(cur_trade["action"], cur_trade["entry"], mid, OZ_FULL)

    return render_template("dashboard.html",
        month      = now.strftime("%b %Y"),
        total      = total,
        wins       = wins,
        losses     = losses,
        partial    = partial,
        count      = count,
        win_rate   = wr,
        best       = best,
        worst      = worst,
        open_trade = cur_trade,
        unrealized = unrealized,
        last_price = cur_price,
        trades     = all_t,
        lot_size   = LOT_SIZE
    )


# ── Status (quick JSON check) ──────────────────────────────────────────
@app.route("/status")
def status():
    with lock:
        return jsonify({"open_trade": open_trade, "last_price": last_price})


# ── Startup (runs on import so gunicorn picks it up too) ───────────────
init_db()
monitor_thread = threading.Thread(target=price_monitor, daemon=True)
monitor_thread.start()

# ── Entry point ────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
