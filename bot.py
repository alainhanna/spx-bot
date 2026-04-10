import requests
import smtplib
import time
import datetime
import pytz
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ─────────────────────────────────────────
# CONFIG — set these as environment variables on Railway
# ─────────────────────────────────────────
POLYGON_API_KEY = os.environ.get("POLYGON_API_KEY", "1u0RUGbackck5ayq2Ab05ErcVPDEs5pl")
ALERT_EMAIL     = os.environ.get("ALERT_EMAIL", "alain.hanna55@gmail.com")
GMAIL_USER      = os.environ.get("GMAIL_USER")      # your Gmail sending address
GMAIL_PASSWORD  = os.environ.get("GMAIL_PASSWORD")  # Gmail App Password

# Signal parameters
PROFIT_TARGET_PCT = 45
STOP_LOSS_PCT     = 50
MIN_CONFIDENCE    = 65
POLL_INTERVAL_SEC = 15  # every 2 minutes
COOLDOWN_MINUTES  = 1
MAX_TRADES_PER_DAY = 25

ET = pytz.timezone("America/New_York")

# ─────────────────────────────────────────
# MARKET HOURS
# ─────────────────────────────────────────
def is_market_open():
    now = datetime.datetime.now(ET)
    if now.weekday() >= 5:
        return False
    o = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    c = now.replace(hour=15, minute=45, second=0, microsecond=0)
    return o <= now <= c

def is_premarket():
    now = datetime.datetime.now(ET)
    if now.weekday() >= 5:
        return False
    s = now.replace(hour=6,  minute=0,  second=0, microsecond=0)
    e = now.replace(hour=9,  minute=29, second=0, microsecond=0)
    return s <= now <= e

def in_entry_window():
    now = datetime.datetime.now(ET).time()
    return (datetime.time(9,0) <= now <= datetime.time(15,30))
           

# ─────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────
def get_spx_bars(limit=60):
    today = datetime.date.today().isoformat()
    url = (
        f"https://api.polygon.io/v2/aggs/ticker/I:SPX/range/1/minute/2020-01-01/{today}"
        f"?adjusted=true&sort=desc&limit={limit}&apiKey={POLYGON_API_KEY}"
    )
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        if data.get("status") in ("OK", "DELAYED") and data.get("results"):
            return list(reversed(data["results"]))
    except Exception as e:
        print(f"[ERROR] bars: {e}")
    return []

def get_vix():
    url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/indices/tickers/I:VIX?apiKey={POLYGON_API_KEY}"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        results = data.get("results", [])
        if results:
            return results[0].get("value")
    except Exception as e:
        print(f"[ERROR] vix: {e}")
    return None

# ─────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────
def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    diffs  = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [d if d > 0 else 0 for d in diffs]
    losses = [-d if d < 0 else 0 for d in diffs]
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100.0
    return round(100 - (100 / (1 + ag / al)), 2)

def calc_vwap(bars):
    tp_vol = sum(((b["h"] + b["l"] + b["c"]) / 3) * b["v"] for b in bars)
    vol    = sum(b["v"] for b in bars)
    return round(tp_vol / vol, 2) if vol else None

def calc_ema(closes, period):
    if len(closes) < period:
        return None
    k   = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]:
        ema = p * k + ema * (1 - k)
    return round(ema, 2)

# ─────────────────────────────────────────
# SIGNAL ENGINE
# ─────────────────────────────────────────
def evaluate_signal(bars):
    if len(bars) < 25:
        return None

    closes = [b["c"] for b in bars]
    highs  = [b["h"] for b in bars]
    lows   = [b["l"] for b in bars]
    spot   = closes[-1]

    rsi   = calc_rsi(closes)
    vwap  = calc_vwap(bars)
    ema9  = calc_ema(closes, 9)
    ema21 = calc_ema(closes, 21)

    if not all([rsi, vwap, ema9, ema21]):
        return None

    candle_range = highs[-1] - lows[-1]
    momentum     = closes[-1] - closes[-2]

    bull_pts = 0
    bear_pts = 0

    # RSI
    if rsi < 32:   bull_pts += 20
    elif rsi < 40: bull_pts += 10
    if rsi > 68:   bear_pts += 20
    elif rsi > 60: bear_pts += 10

    # VWAP
    if spot > vwap: bull_pts += 12
    else:           bear_pts += 12

    # EMA cross
    if ema9 > ema21: bull_pts += 10
    else:            bear_pts += 10

    # Momentum
    if momentum > 0: bull_pts += 8
    else:            bear_pts += 8

    # Price vs EMA9
    if spot > ema9: bull_pts += 5
    else:           bear_pts += 5

    total = bull_pts + bear_pts
    if total == 0:
        return None

    if bull_pts > bear_pts:
        bias        = "BULL"
        option_type = "CALL"
        confidence  = round(50 + (bull_pts - bear_pts) / total * 50)
        strike      = round((spot + spot * 0.002) / 5) * 5
        invalidate  = round(vwap - candle_range * 0.5, 2)
    else:
        bias        = "BEAR"
        option_type = "PUT"
        confidence  = round(50 + (bear_pts - bull_pts) / total * 50)
        strike      = round((spot - spot * 0.002) / 5) * 5
        invalidate  = round(vwap + candle_range * 0.5, 2)

    if confidence < MIN_CONFIDENCE:
        return None

    return {
        "bias": bias, "option_type": option_type,
        "confidence": confidence, "spot": round(spot, 2),
        "strike": strike, "vwap": vwap, "rsi": rsi,
        "ema9": ema9, "ema21": ema21, "invalidate": invalidate,
    }

# ─────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────
def send_email(subject, body):
    if not GMAIL_USER or not GMAIL_PASSWORD:
        print(f"\n{'='*50}\n[NO EMAIL CONFIG — printing alert]\n{subject}\n{body}\n{'='*50}")
        return
    try:
        msg = MIMEMultipart()
        msg["Subject"] = subject
        msg["From"]    = GMAIL_USER
        msg["To"]      = ALERT_EMAIL
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_PASSWORD)
            s.sendmail(GMAIL_USER, ALERT_EMAIL, msg.as_string())
        print(f"[EMAIL SENT] {subject}")
    except Exception as e:
        print(f"[ERROR] email failed: {e}")

def format_signal(sig):
    t     = datetime.datetime.now(ET).strftime("%I:%M %p ET")
    emoji = "🟢" if sig["bias"] == "BULL" else "🔴"
    subj  = f"SPX SIGNAL {emoji} {sig['option_type']} — {t}"
    body  = f"""
SPX SIGNAL {emoji} {sig['option_type']}
━━━━━━━━━━━━━━━━━━━━━━━━
Time:       {t}
Spot:       {sig['spot']:,.2f}
Strike:     {sig['strike']} {sig['option_type']} 0DTE
Confidence: {sig['confidence']}%
Target:     +{PROFIT_TARGET_PCT}% of premium
Stop:       -{STOP_LOSS_PCT}% of premium
Invalidate: {sig['invalidate']:,.2f}
━━━━━━━━━━━━━━━━━━━━━━━━
RSI:   {sig['rsi']}
VWAP:  {sig['vwap']:,.2f}
EMA9:  {sig['ema9']:,.2f}
EMA21: {sig['ema21']:,.2f}
━━━━━━━━━━━━━━━━━━━━━━━━
Execute manually in Robinhood.
Max 3 trades today.
"""
    return subj, body

def send_premarket_brief():
    bars = get_spx_bars(limit=100)
    vix  = get_vix()
    if not bars:
        return
    spot        = bars[-1]["c"]
    vwap        = calc_vwap(bars)
    closes      = [b["c"] for b in bars]
    rsi         = calc_rsi(closes)
    today_open  = bars[-1]["o"]
    prior_close = bars[-2]["c"] if len(bars) > 1 else spot
    gap_pct     = round(((today_open - prior_close) / prior_close) * 100, 3)
    gap_label   = "FLAT" if abs(gap_pct) < 0.1 else ("GAP UP ▲" if gap_pct > 0 else "GAP DOWN ▼")
    regime      = "Mean-revert favored" if abs(gap_pct) < 0.15 else ("Momentum UP" if gap_pct > 0 else "Momentum DOWN")
    vix_str     = f"{vix:.2f}" if vix else "N/A"

    subj = f"☀️ SPX Pre-Market Brief — {datetime.date.today().strftime('%b %d, %Y')}"
    body = f"""
☀️ SPX PRE-MARKET BRIEF
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{datetime.date.today().strftime('%A, %B %d, %Y')}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Spot (last):  {spot:,.2f}
VWAP:         {vwap:,.2f}
RSI(14):      {rsi}
VIX:          {vix_str}
Gap:          {gap_pct:+.3f}%  {gap_label}
Regime:       {regime}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Entry Windows: 09:45–11:30 ET | 13:00–14:30 ET
Max Trades:    3/day
Min Confidence: {MIN_CONFIDENCE}%
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Signal alerts fire automatically during RTH.
"""
    send_email(subj, body)

# ─────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────
def main():
    print("=" * 50)
    print("  SPX 0DTE Signal Bot")
    print(f"  Alerts → {ALERT_EMAIL}")
    print("=" * 50)

    trade_count      = 0
    last_signal_time = None
    last_signal_bias = None
    premarket_sent   = False
    last_date        = None

    while True:
        now_et = datetime.datetime.now(ET)
        today  = now_et.date()

        # Daily reset at midnight
        if last_date != today:
            trade_count      = 0
            last_signal_time = None
            last_signal_bias = None
            premarket_sent   = False
            last_date        = today
            print(f"\n[{now_et.strftime('%H:%M ET')}] New day — counters reset.")

        # Pre-market brief at 6 AM ET
        if is_premarket() and not premarket_sent and now_et.hour >= 6:
            print(f"[{now_et.strftime('%H:%M ET')}] Sending pre-market brief...")
            send_premarket_brief()
            premarket_sent = True

        # RTH signal loop
        if is_market_open():
            if trade_count >= MAX_TRADES_PER_DAY
                print(f"[{now_et.strftime('%H:%M ET')}] Max 3 trades hit. Done for today.")
            elif not in_entry_window():
                print(f"[{now_et.strftime('%H:%M ET')}] Outside entry window.")
            else:
                # Cooldown check
                cooldown_ok = True
                if last_signal_time:
                    mins = (now_et - last_signal_time).total_seconds() / 60
                    if mins < COOLDOWN_MINUTES:
                        cooldown_ok = False
                        print(f"[{now_et.strftime('%H:%M ET')}] Cooldown: {COOLDOWN_MINUTES - int(mins)}m remaining.")

                if cooldown_ok:
                    print(f"[{now_et.strftime('%H:%M ET')}] Scanning... SPX trade {trade_count+1}/3")
                    bars = get_spx_bars(limit=60)
                    if bars and len(bars) >= 25:
                        sig = evaluate_signal(bars)
                        if sig:
                            if sig["bias"] == last_signal_bias:
                                print(f"[{now_et.strftime('%H:%M ET')}] Same direction as last — skipping.")
                            else:
                                subj, body = format_signal(sig)
                                send_email(subj, body)
                                print(body)
                                trade_count     += 1
                                last_signal_time = now_et
                                last_signal_bias = sig["bias"]
                        else:
                            print(f"[{now_et.strftime('%H:%M ET')}] No signal. SPX={bars[-1]['c']:,.2f}")
                    else:
                        print(f"[{now_et.strftime('%H:%M ET')}] Not enough bars yet.")
        else:
            if not is_premarket():
                print(f"[{now_et.strftime('%H:%M ET')}] Market closed.")

        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    main()
