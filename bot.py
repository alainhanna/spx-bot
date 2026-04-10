import requests
import smtplib
import time
import datetime
import pytz
import os
import threading
from flask import Flask
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
POLYGON_API_KEY  = os.environ.get("POLYGON_API_KEY", "1u0RUGbackck5ayq2Ab05ErcVPDEs5pl")
ALERT_EMAIL      = os.environ.get("ALERT_EMAIL", "alain.hanna55@gmail.com")
GMAIL_USER       = os.environ.get("GMAIL_USER")
GMAIL_PASSWORD   = os.environ.get("GMAIL_PASSWORD")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "8796616207:AAEUsEl45pRz92mYXVSUIEFUW1t-CNepGdY")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "6459251326")

# Signal parameters
PROFIT_TARGET_PCT    = 45
STOP_LOSS_PCT        = 50
MIN_CONFIDENCE       = 60
MIN_CONFIDENCE_HIGH_VIX = 70   # tightens automatically when VIX > 25
VIX_HIGH_THRESHOLD   = 25
POLL_INTERVAL_SEC    = 15
COOLDOWN_MINUTES     = 1
MAX_TRADES_PER_DAY   = 25

# VIX Spike parameters
VIX_SPIKE_LEVELS     = [20, 25, 30]   # alert when VIX crosses these
VIX_REVERSAL_POINTS  = 1.5            # VIX must drop this much from high to fire CALL signal
VIX_ALLCLEAR_LEVEL   = 20             # VIX dropping below this fires all-clear

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
    return datetime.time(9, 0) <= now <= datetime.time(15, 30)

# ─────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────
def get_spx_bars(limit=60):
    today = datetime.date.today().isoformat()
    url = (
        f"https://api.polygon.io/v2/aggs/ticker/I:SPX/range/1/minute/2020-01-01/{today}"
        f"?adjusted=true&sort=desc&limit={limit}&apiKey={POLYGON_API_KEY}"
    )
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=10)
            data = r.json()
            if data.get("status") in ("OK", "DELAYED") and data.get("results"):
                return list(reversed(data["results"]))
        except Exception as e:
            if attempt < 2:
                print(f"[WARN] bars fetch attempt {attempt+1} failed, retrying...")
                time.sleep(2)
            else:
                print(f"[ERROR] bars after 3 attempts: {e}")
    return []

def get_vix():
    """Try multiple endpoints to get VIX value, with retry logic"""
    for attempt in range(3):
        # Try 1: Indices snapshot
        try:
            url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/indices/tickers/I:VIX?apiKey={POLYGON_API_KEY}"
            r = requests.get(url, timeout=10)
            text = r.text.strip()
            if text.startswith("{"):
                data = r.json()
                results = data.get("results", [])
                if results:
                    val = results[0].get("value") or results[0].get("last", {}).get("value")
                    if val is not None:
                        return float(val)
        except Exception:
            pass

        # Try 2: Previous close via aggregates
        try:
            today = datetime.date.today().isoformat()
            url = f"https://api.polygon.io/v2/aggs/ticker/I:VIX/range/1/day/2020-01-01/{today}?adjusted=true&sort=desc&limit=1&apiKey={POLYGON_API_KEY}"
            r = requests.get(url, timeout=10)
            data = r.json()
            if data.get("results"):
                return float(data["results"][0]["c"])
        except Exception as e:
            if attempt < 2:
                print(f"[WARN] VIX fetch attempt {attempt+1} failed, retrying...")
                time.sleep(2)
            else:
                print(f"[ERROR] vix fallback after 3 attempts: {e}")

    return None


def get_spx_options_chain():
    """
    Fetch SPX options chain to calculate GEX proxy.
    Returns list of option contracts with strike, expiry, OI, gamma.
    """
    today = datetime.date.today().isoformat()
    url = (
        f"https://api.polygon.io/v3/snapshot/options/I:SPX"
        f"?expiration_date={today}&limit=250&apiKey={POLYGON_API_KEY}"
    )
    try:
        r = requests.get(url, timeout=15)
        data = r.json()
        if data.get("status") in ("OK", "DELAYED") and data.get("results"):
            return data["results"]
    except Exception as e:
        print(f"[ERROR] options chain: {e}")
    return []

# ─────────────────────────────────────────
# ECONOMIC CALENDAR
# ─────────────────────────────────────────
# High impact events — hardcoded keywords to flag
HIGH_IMPACT_KEYWORDS = [
    "consumer price index", "cpi",
    "federal reserve", "fomc", "fed rate", "interest rate decision",
    "nonfarm payroll", "nfp", "jobs report",
    "gdp", "gross domestic product",
    "pce", "personal consumption",
    "producer price index", "ppi",
    "unemployment rate", "jobless claims",
    "retail sales", "consumer sentiment",
    "ism manufacturing", "ism services"
]

def get_economic_events():
    """
    Get today's high-impact economic events.
    Uses two sources:
    1. MANUAL_EVENTS — hardcoded events you add yourself (always checked first)
    2. financialmodelingprep API — auto-fetched if available
    """
    today     = datetime.date.today()
    today_str = today.isoformat()
    events    = []

    # ── SOURCE 1: MANUAL OVERRIDE ──────────────────────────────
    # Add events here yourself each week. Format: "HH:MM" in ET.
    # Example: MANUAL_EVENTS = [("08:30", "CPI"), ("14:00", "FOMC")]
    # Clear this list when the events have passed.
    MANUAL_EVENTS = [
        ("08:30", "CPI — Consumer Price Index"),
        ("10:00", "Consumer Sentiment"),
    ]

    for time_str, name in MANUAL_EVENTS:
        try:
            h, m = map(int, time_str.split(":"))
            event_dt = ET.localize(datetime.datetime(today.year, today.month, today.day, h, m))
            events.append({"name": name, "time": event_dt, "impact": "high"})
        except Exception:
            continue

    # ── SOURCE 2: API (backup) ──────────────────────────────────
    try:
        url = f"https://financialmodelingprep.com/api/v3/economic_calendar?from={today_str}&to={today_str}&apikey=demo"
        r   = requests.get(url, timeout=10)
        if r.status_code == 200:
            api_events = r.json()
            if isinstance(api_events, list):
                for e in api_events:
                    name   = (e.get("event") or "").lower()
                    impact = (e.get("impact") or "").lower()
                    date   = e.get("date") or ""
                    is_high    = impact in ("high", "critical")
                    is_keyword = any(k in name for k in HIGH_IMPACT_KEYWORDS)
                    if (is_high or is_keyword) and date:
                        try:
                            event_dt = datetime.datetime.strptime(date, "%Y-%m-%d %H:%M:%S")
                            event_et = ET.localize(event_dt)
                            # Don't duplicate manual events
                            already = any(abs((ev["time"] - event_et).total_seconds()) < 300 for ev in events)
                            if not already:
                                events.append({"name": e.get("event", "Unknown"), "time": event_et, "impact": impact})
                        except Exception:
                            continue
    except Exception as e:
        print(f"[CALENDAR] API unavailable: {e}")

    print(f"[CALENDAR] {len(events)} high-impact events today: {[e['name'] for e in events]}")
    return events

def check_event_blackout(events, window_minutes=30):
    """
    Returns (in_blackout, event_name, minutes_away) if within window of a high-impact event.
    Blackout: 30 min before AND 15 min after each event.
    """
    now = datetime.datetime.now(ET)
    for event in events:
        event_time = event["time"]
        mins_until  = (event_time - now).total_seconds() / 60
        mins_since  = (now - event_time).total_seconds() / 60

        # Blackout 30 min before
        if 0 <= mins_until <= window_minutes:
            return True, event["name"], round(mins_until), "before"

        # Blackout 1 min after
        if 0 <= mins_since <= 1:
            return True, event["name"], round(mins_since), "after"

    return False, None, None, None

def get_momentum_bias_post_event(bars):
    """
    After a high-impact event, determine momentum direction from price action.
    Returns 'BULL', 'BEAR', or None if not clear enough.
    """
    if len(bars) < 5:
        return None
    recent   = bars[-1]["c"]
    baseline = bars[-5]["c"]
    move_pct = (recent - baseline) / baseline * 100

    if move_pct > 0.3:
        return "BULL"
    elif move_pct < -0.3:
        return "BEAR"
    return None

def calculate_gex(options_chain, spot):
    """
    OI-based gravity levels as GEX proxy (gamma not available on free Polygon tier).
    - call_oi - put_oi per strike = net dealer exposure proxy
    - GEX zero = strike where net OI flips from positive to negative
    - Top levels = highest total OI strikes (max pain / pin risk)
    Returns: gex_zero (flip level), top_strikes list, total_oi
    """
    if not options_chain:
        return None, [], 0

    call_oi_by_strike = {}
    put_oi_by_strike  = {}
    total_oi = 0

    for contract in options_chain:
        try:
            details = contract.get("details", {})
            strike  = details.get("strike_price", 0)
            cp      = details.get("contract_type", "").lower()
            oi      = contract.get("open_interest", 0) or 0

            if not strike or not oi:
                continue

            if cp == "call":
                call_oi_by_strike[strike] = call_oi_by_strike.get(strike, 0) + oi
            elif cp == "put":
                put_oi_by_strike[strike]  = put_oi_by_strike.get(strike, 0) + oi
            total_oi += oi

        except Exception:
            continue

    all_strikes = sorted(set(list(call_oi_by_strike.keys()) + list(put_oi_by_strike.keys())))

    if not all_strikes:
        return None, [], 0

    # Net OI per strike (calls - puts) — flip point = GEX zero proxy
    net_oi = {}
    for s in all_strikes:
        net_oi[s] = call_oi_by_strike.get(s, 0) - put_oi_by_strike.get(s, 0)

    # Find flip point closest to spot
    gex_zero = spot
    strikes_near_spot = [s for s in all_strikes if abs(s - spot) <= 200]
    for i in range(len(strikes_near_spot) - 1):
        s1, s2 = strikes_near_spot[i], strikes_near_spot[i+1]
        if net_oi.get(s1, 0) > 0 and net_oi.get(s2, 0) <= 0:
            gex_zero = s2
            break
        elif net_oi.get(s1, 0) < 0 and net_oi.get(s2, 0) >= 0:
            gex_zero = s2
            break

    # Top OI levels = highest total OI (call + put) near spot
    total_oi_by_strike = {}
    for s in all_strikes:
        if abs(s - spot) <= 150:
            total_oi_by_strike[s] = call_oi_by_strike.get(s, 0) + put_oi_by_strike.get(s, 0)

    top_levels  = sorted(total_oi_by_strike.items(), key=lambda x: x[1], reverse=True)[:5]
    top_strikes = sorted([s for s, _ in top_levels])

    print(f"[GEX] Zero={gex_zero} | Top OI strikes={top_strikes} | Total OI={total_oi:,}")
    return round(gex_zero, 0), top_strikes, round(total_oi, 0)

# ─────────────────────────────────────────
# TREND DETECTION
# ─────────────────────────────────────────
def detect_trend(bars):
    """
    Fast momentum-based trend detection using last 10 bars.
    Catches moves as they happen, not after session open comparison.
    Returns: 'BULL', 'BEAR', or 'NEUTRAL'
    """
    if len(bars) < 10:
        return "NEUTRAL"

    recent = bars[-10:]
    closes = [b["c"] for b in recent]

    # Short-term momentum: compare last 3 bars to 10 bars ago
    momentum_3  = closes[-1] - closes[-3]
    momentum_10 = closes[-1] - closes[0]

    # EMA slope — is the 5-bar EMA rising or falling?
    ema5 = closes[-1]
    k = 2 / (5 + 1)
    for c in reversed(closes[-5:]):
        ema5 = c * k + ema5 * (1 - k)
    ema5_prev = closes[-5]
    k2 = 2 / (5 + 1)
    for c in reversed(closes[-9:-4]):
        ema5_prev = c * k2 + ema5_prev * (1 - k2)

    ema_rising  = ema5 > ema5_prev
    ema_falling = ema5 < ema5_prev

    # Count green vs red bars in last 10
    green = sum(1 for b in recent if b["c"] > b["o"])
    red   = sum(1 for b in recent if b["c"] < b["o"])

    bull_signals = 0
    bear_signals = 0

    if momentum_3 > 1:  bull_signals += 1
    if momentum_3 < -1: bear_signals += 1
    if momentum_10 > 3: bull_signals += 1
    if momentum_10 < -3: bear_signals += 1
    if ema_rising:  bull_signals += 1
    if ema_falling: bear_signals += 1
    if green >= 7:  bull_signals += 1
    if red >= 7:    bear_signals += 1

    if bull_signals >= 3:
        return "BULL"
    elif bear_signals >= 3:
        return "BEAR"
    else:
        return "NEUTRAL"

# ─────────────────────────────────────────
# VIX SPIKE MONITOR
# ─────────────────────────────────────────
def check_vix_spike(vix, vix_history, last_vix_alerts):
    """
    Monitor VIX for spikes and reversals.
    Returns list of alerts to send, each is a dict with type and message.
    vix_history: list of recent VIX values (last 20 readings)
    last_vix_alerts: set of levels already alerted to avoid duplicates
    """
    if vix is None or len(vix_history) < 2:
        return []

    alerts = []
    prev_vix = vix_history[-2] if len(vix_history) >= 2 else vix

    # Check for level crossings
    for level in VIX_SPIKE_LEVELS:
        key_up   = f"cross_up_{level}"
        key_down = f"cross_down_{level}"

        # VIX crossed UP through level
        if prev_vix < level <= vix and key_up not in last_vix_alerts:
            alerts.append({
                "type": "SPIKE",
                "level": level,
                "vix": vix,
                "direction": "UP"
            })
            last_vix_alerts.add(key_up)
            last_vix_alerts.discard(key_down)  # reset down alert

        # VIX crossed DOWN through level
        if prev_vix > level >= vix and key_down not in last_vix_alerts:
            alerts.append({
                "type": "CROSS_DOWN",
                "level": level,
                "vix": vix,
                "direction": "DOWN"
            })
            last_vix_alerts.add(key_down)
            last_vix_alerts.discard(key_up)

    # Check for reversal — VIX dropped VIX_REVERSAL_POINTS from recent high
    if len(vix_history) >= 5:
        recent_high = max(vix_history[-10:]) if len(vix_history) >= 10 else max(vix_history)
        reversal_key = f"reversal_{round(recent_high)}"
        if (recent_high >= 20 and
            recent_high - vix >= VIX_REVERSAL_POINTS and
            reversal_key not in last_vix_alerts):
            alerts.append({
                "type": "REVERSAL",
                "vix": vix,
                "vix_high": recent_high,
                "drop": round(recent_high - vix, 2)
            })
            last_vix_alerts.add(reversal_key)

    return alerts

def format_vix_alert(alert, spot=None):
    t = datetime.datetime.now(ET).strftime("%I:%M %p ET")

    if alert["type"] == "SPIKE":
        subj = f"⚠️ VIX SPIKE ALERT — {alert['vix']:.1f} crossed {alert['level']}"
        body = f"""
VIX SPIKE ALERT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Time:     {t}
VIX:      {alert['vix']:.2f}  ▲ crossed {alert['level']}
SPX:      {spot:,.2f}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Volatility expanding. Be cautious on new entries.
Wait for VIX to reverse before fading the move.
Normal signals require {MIN_CONFIDENCE_HIGH_VIX}%+ confidence now.
"""

    elif alert["type"] == "CROSS_DOWN":
        subj = f"✅ VIX CALMING — {alert['vix']:.1f} dropped below {alert['level']}"
        body = f"""
VIX CALMING ALERT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Time:     {t}
VIX:      {alert['vix']:.2f}  ▼ below {alert['level']}
SPX:      {spot:,.2f}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Volatility contracting. Normal conditions resuming.
Signal confidence threshold back to {MIN_CONFIDENCE}%.
"""

    elif alert["type"] == "REVERSAL":
        strike = round((spot + spot * 0.002) / 5) * 5 if spot else "N/A"
        subj = f"🟢 VIX REVERSAL — Mean Reversion CALL Setup"
        body = f"""
VIX REVERSAL — CALL SETUP
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Time:       {t}
VIX High:   {alert['vix_high']:.2f}
VIX Now:    {alert['vix']:.2f}  (dropped {alert['drop']} pts)
SPX:        {spot:,.2f}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
VIX REVERSAL SETUP:
Strike:     {strike} CALL 0DTE
Thesis:     Fear peaked, mean reversion rally likely
Target:     +45% of premium
Invalidate: VIX makes new high
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Execute manually in Robinhood.
This is a VIX-driven signal, independent of RSI/VWAP.
"""
    else:
        return None, None

    return subj, body



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
    try:
        tp_vol = sum(((b["h"] + b["l"] + b["c"]) / 3) * b.get("v", 0) for b in bars)
        vol    = sum(b.get("v", 0) for b in bars)
        return round(tp_vol / vol, 2) if vol else None
    except Exception:
        return None

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
def evaluate_signal(bars, vix=None, gex_zero=None, intraday_trend="NEUTRAL"):
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

    # Determine effective confidence threshold based on VIX
    effective_min_confidence = MIN_CONFIDENCE
    if vix and vix > VIX_HIGH_THRESHOLD:
        effective_min_confidence = MIN_CONFIDENCE_HIGH_VIX
        print(f"  [VIX FILTER] VIX={vix:.1f} > {VIX_HIGH_THRESHOLD} — raising min confidence to {MIN_CONFIDENCE_HIGH_VIX}%")

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

    # Trend confluence bonus
    if intraday_trend == "BULL": bull_pts += 10
    elif intraday_trend == "BEAR": bear_pts += 10

    total = bull_pts + bear_pts
    if total == 0:
        return None

    if bull_pts > bear_pts:
        bias        = "BULL"
        option_type = "CALL"
        confidence  = round(50 + (bull_pts - bear_pts) / total * 50)
    else:
        bias        = "BEAR"
        option_type = "PUT"
        confidence  = round(50 + (bear_pts - bull_pts) / total * 50)

    if confidence < effective_min_confidence:
        return None

    # Strike calculation
    strike_offset = spot * 0.002
    if bias == "BULL":
        strike     = round((spot + strike_offset) / 5) * 5
        invalidate = round(vwap - candle_range * 0.5, 2)
    else:
        strike     = round((spot - strike_offset) / 5) * 5
        invalidate = round(vwap + candle_range * 0.5, 2)

    return {
        "bias": bias, "option_type": option_type,
        "confidence": min(confidence, 95), "spot": round(spot, 2),
        "strike": strike, "vwap": vwap, "rsi": rsi,
        "ema9": ema9, "ema21": ema21, "invalidate": invalidate,
        "gex_zero": None, "intraday_trend": intraday_trend,
        "vix": vix, "high_vix": vix > VIX_HIGH_THRESHOLD if vix else False,
    }

# ─────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────
def send_alert(subject, body):
    """Send alert via Telegram (primary) and email (fallback)"""
    message = f"*{subject}*\n\n```{body}```"
    # Try Telegram first
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": f"{subject}\n\n{body}",
            "parse_mode": "Markdown"
        }
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            print(f"[TELEGRAM SENT] {subject}")
            return
        else:
            print(f"[TELEGRAM ERROR] {r.status_code}: {r.text}")
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")

    # Fallback to email
    send_email(subject, body)

def send_email(subject, body):
    """Send alert via Telegram — works on Railway (HTTPS not SMTP)"""
    TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "8796616207:AAEUsEl45pRz92mYXVSUIEFUW1t-CNepGdY")
    TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "6459251326")
    message = f"{subject}\n\n{body}"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message
        }, timeout=10)
        if r.status_code == 200:
            print(f"[TELEGRAM SENT] {subject}")
        else:
            print(f"[ERROR] telegram: {r.status_code} {r.text}")
    except Exception as e:
        print(f"[ERROR] telegram: {e}")

def format_signal(sig):
    t     = datetime.datetime.now(ET).strftime("%I:%M %p ET")
    emoji = "🟢" if sig["bias"] == "BULL" else "🔴"
    subj  = f"SPX SIGNAL {emoji} {sig['option_type']} — {t}"

    # VIX warning
    vix_line = ""
    if sig.get("high_vix") and sig.get("vix"):
        vix_line = f"⚠️ HIGH VIX ({sig['vix']:.1f}) — confidence threshold raised to {MIN_CONFIDENCE_HIGH_VIX}%\n"

    # GEX line
    gex_line = f"GEX Zero: {sig['gex_zero']:,.0f}" if sig.get("gex_zero") else "GEX Zero: N/A"

    # Trend line
    trend_emoji = "📈" if sig["intraday_trend"] == "BULL" else "📉" if sig["intraday_trend"] == "BEAR" else "➡️"
    trend_line  = f"Trend:    {trend_emoji} {sig['intraday_trend']}"

    body = f"""
SPX SIGNAL {emoji} {sig['option_type']}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{vix_line}Time:       {t}
Spot:       {sig['spot']:,.2f}
Strike:     {sig['strike']} {sig['option_type']} 0DTE
Confidence: {sig['confidence']}%
Target:     +{PROFIT_TARGET_PCT}% of premium
Stop:       -{STOP_LOSS_PCT}% of premium
Invalidate: {sig['invalidate']:,.2f}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RSI:   {sig['rsi']}
VWAP:  {sig['vwap']:,.2f}
EMA9:  {sig['ema9']:,.2f}
EMA21: {sig['ema21']:,.2f}
{gex_line}
{trend_line}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Execute manually in Robinhood.
"""
    return subj, body

def send_premarket_brief(vix=None, gex_zero=None, top_gex_levels=None, today_events=None):
    bars = get_spx_bars(limit=100)
    if not bars:
        return
    try:
        spot        = bars[-1]["c"]
        vwap        = calc_vwap(bars)
        closes      = [b["c"] for b in bars]
        rsi         = calc_rsi(closes)
        today_open  = bars[-1].get("o", spot)
        prior_close = bars[-2]["c"] if len(bars) > 1 else spot
        gap_pct     = round(((today_open - prior_close) / prior_close) * 100, 3)
        gap_label   = "FLAT" if abs(gap_pct) < 0.1 else ("GAP UP" if gap_pct > 0 else "GAP DOWN")
        regime      = "Mean-revert favored" if abs(gap_pct) < 0.15 else ("Momentum UP" if gap_pct > 0 else "Momentum DOWN")
        vix_val     = float(vix) if vix is not None else None
        vix_str     = f"{vix_val:.2f}" if vix_val is not None else "N/A"
        vix_warn    = " HIGH VIX — signals require 75%+ confidence" if vix_val and vix_val > VIX_HIGH_THRESHOLD else ""
        gex_str     = f"{gex_zero:,.0f}" if gex_zero else "N/A"
        levels_str  = ", ".join([f"{l:,.0f}" for l in top_gex_levels]) if top_gex_levels else "N/A"
        vwap_str    = f"{vwap:,.2f}" if vwap else "N/A"
        rsi_str     = str(rsi) if rsi else "N/A"
        conf_today  = MIN_CONFIDENCE_HIGH_VIX if vix_val and vix_val > VIX_HIGH_THRESHOLD else MIN_CONFIDENCE

        subj = f"SPX Pre-Market Brief — {datetime.date.today().strftime('%b %d, %Y')}"

        # Format calendar events
        events_str = "None"
        if today_events:
            event_lines = []
            for e in today_events:
                t = e["time"].strftime("%I:%M %p ET")
                event_lines.append(f"  {t}  {e['name']}")
            events_str = "\n".join(event_lines)

        body = f"""
SPX PRE-MARKET BRIEF
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{datetime.date.today().strftime('%A, %B %d, %Y')}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Spot (last):  {spot:,.2f}
VWAP:         {vwap_str}
RSI(14):      {rsi_str}
VIX:          {vix_str}{vix_warn}
Gap:          {gap_pct:+.3f}%  {gap_label}
Regime:       {regime}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GEX Zero:     {gex_str}
Key GEX Levels: {levels_str}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HIGH IMPACT EVENTS TODAY:
{events_str}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Scanning:     9:00 AM - 3:30 PM ET
Max Trades:   {MAX_TRADES_PER_DAY}/day
Confidence:   {conf_today}%+ required today
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Signal alerts fire automatically during RTH.
Signals suppressed 30min before / 1min after high-impact events.
"""
        send_email(subj, body)
    except Exception as e:
        print(f"[ERROR] premarket brief: {e}")

# ─────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────
def main():
    print("=" * 50)
    print("  SPX 0DTE Signal Bot v2")
    print(f"  Alerts → {ALERT_EMAIL}")
    print("=" * 50)

    trade_count      = 0
    last_signal_time = None
    last_signal_bias = None
    premarket_sent   = False
    last_date        = None

    # Cache GEX — refresh every 30 minutes
    gex_zero       = None
    top_gex_levels = []
    last_gex_fetch = None
    GEX_REFRESH_MINS = 30

    # VIX spike tracking
    vix_history     = []
    last_vix_alerts = set()

    # Economic calendar
    today_events = []

    while True:
        now_et = datetime.datetime.now(ET)
        today  = now_et.date()

        # Daily reset
        if last_date != today:
            trade_count      = 0
            last_signal_time = None
            last_signal_bias = None
            premarket_sent   = False
            last_date        = today
            gex_zero         = None
            top_gex_levels   = []
            last_gex_fetch   = None
            vix_history      = []
            last_vix_alerts  = set()
            today_events     = []
            print(f"\n[{now_et.strftime('%H:%M ET')}] New day — counters reset.")

        # Fetch VIX and update history
        vix = get_vix()
        if vix is not None:
            vix_history.append(vix)
            if len(vix_history) > 20:
                vix_history.pop(0)

        # Pre-market brief at 6 AM ET
        if is_premarket() and not premarket_sent and now_et.hour >= 6:
            print(f"[{now_et.strftime('%H:%M ET')}] Fetching economic calendar...")
            today_events = get_economic_events()
            print(f"[{now_et.strftime('%H:%M ET')}] Found {len(today_events)} high-impact events today.")
            print(f"[{now_et.strftime('%H:%M ET')}] Sending pre-market brief...")
            send_premarket_brief(vix=vix, gex_zero=None, top_gex_levels=[], today_events=today_events)
            premarket_sent = True

        # RTH signal loop
        if is_market_open():

            # VIX spike monitor — runs every scan regardless of entry window
            if len(vix_history) >= 2:
                vix_alerts = check_vix_spike(vix, vix_history, last_vix_alerts)
                for alert in vix_alerts:
                    bars_spot = get_spx_bars(limit=3)
                    spot_now  = bars_spot[-1]["c"] if bars_spot else None
                    subj, body = format_vix_alert(alert, spot=spot_now)
                    if subj and body:
                        send_email(subj, body)
                        print(f"\n[VIX ALERT] {subj}")
                        print(body)

            # Economic calendar blackout check
            in_blackout, event_name, mins_away, when = check_event_blackout(today_events)
            if in_blackout:
                print(f"[{now_et.strftime('%H:%M ET')}] BLACKOUT — {event_name} ({mins_away}min {when}). No signals.")
                time.sleep(POLL_INTERVAL_SEC)
                continue

            if trade_count >= MAX_TRADES_PER_DAY:
                print(f"[{now_et.strftime('%H:%M ET')}] Max {MAX_TRADES_PER_DAY} trades hit. Done for today.")
            elif not in_entry_window():
                print(f"[{now_et.strftime('%H:%M ET')}] Outside entry window.")
            else:
                cooldown_ok = True
                if last_signal_time:
                    mins = (now_et - last_signal_time).total_seconds() / 60
                    if mins < COOLDOWN_MINUTES:
                        cooldown_ok = False

                if cooldown_ok:
                    bars = get_spx_bars(limit=60)
                    if bars and len(bars) >= 25:
                        intraday_trend = detect_trend(bars)
                        sig = evaluate_signal(
                            bars,
                            vix=vix,
                            gex_zero=gex_zero,
                            intraday_trend=intraday_trend
                        )
                        spot = bars[-1]["c"]
                        vix_str = f"{vix:.1f}" if vix else "N/A"
                        print(f"[{now_et.strftime('%H:%M ET')}] SPX={spot:,.2f} VIX={vix_str} GEX0={gex_zero} Trend={intraday_trend} Trade={trade_count+1}/{MAX_TRADES_PER_DAY}")

                        if sig:
                            if sig["bias"] == last_signal_bias:
                                print(f"  Same direction as last signal — skipping.")
                            else:
                                subj, body = format_signal(sig)
                                send_alert(subj, body)
                                print(body)
                                trade_count     += 1
                                last_signal_time = now_et
                                last_signal_bias = sig["bias"]
                    else:
                        print(f"[{now_et.strftime('%H:%M ET')}] Not enough bars.")
        else:
            if not is_premarket():
                print(f"[{now_et.strftime('%H:%M ET')}] Market closed.")

        time.sleep(POLL_INTERVAL_SEC)

# ─────────────────────────────────────────
# HEALTH CHECK SERVER
# ─────────────────────────────────────────
app = Flask(__name__)

@app.route("/health")
def health():
    return {"status": "ok", "bot": "SPX 0DTE Signal Bot v2"}, 200

@app.route("/")
def index():
    return {"status": "running"}, 200

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

if __name__ == "__main__":
    # Run health check server in background thread
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    print(f"[HEALTH] Health check server started on port {os.environ.get('PORT', 8080)}")
    # Run bot main loop
    main()
