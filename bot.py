import requests
import time
import datetime
import pytz
import os
import threading
try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False
from flask import Flask

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
POLYGON_API_KEY  = os.environ.get("POLYGON_API_KEY")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Fail loudly if credentials missing
if not POLYGON_API_KEY:
    raise ValueError("POLYGON_API_KEY environment variable not set")
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN environment variable not set")
if not TELEGRAM_CHAT_ID:
    raise ValueError("TELEGRAM_CHAT_ID environment variable not set")

POLL_INTERVAL_SEC  = 15
COOLDOWN_MINUTES   = 1
MAX_ALERTS_PER_DAY = 20
PROFIT_TARGET_PCT  = 45
STOP_LOSS_PCT      = 50

# Key level proximity — how close to a level before we watch for reaction (points)
LEVEL_PROXIMITY    = 8

# Momentum: minimum points moved in last N bars to confirm direction
MOMENTUM_BARS      = 3
MOMENTUM_MIN_MOVE  = 3.0   # SPX points

# Hardening rules
MIN_SIGNAL_DISTANCE  = 6    # SPX points — direction-aware price cooldown
VWAP_FLIP_COOLDOWN   = 5    # minutes — prevents VWAP reclaim/rejection spam
VWAP_MAX_EXTENSION   = 50   # SPX points — suppress ALL signals beyond this
VWAP_BREAK_EXTENSION = 25   # SPX points — suppress breakout/momentum only
MAX_SIGNAL_SCORE     = 18   # score cap to prevent runaway scoring

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
    return datetime.time(9, 30) <= now <= datetime.time(15, 30)

# ─────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────
def get_spx_bars_yfinance(limit=60):
    if not YFINANCE_AVAILABLE:
        return []
    try:
        spx = yf.download("^GSPC", period="2d", interval="1m", progress=False)
        if spx.empty:
            return []
        # Filter to today only
        today = datetime.datetime.now(ET).date()
        spx = spx[spx.index.date == today]
        spx = spx.tail(limit)
        bars = []
        for ts, row in spx.iterrows():
            def v(x):
                try:
                    return float(x.iloc[0]) if hasattr(x, 'iloc') else float(x)
                except:
                    return 0.0
            bars.append({
                "o": v(row["Open"]),
                "h": v(row["High"]),
                "l": v(row["Low"]),
                "c": v(row["Close"]),
                "v": v(row["Volume"]),
            })
        if bars:
            print(f"[YFINANCE] {len(bars)} bars fetched as fallback")
        return bars
    except Exception as e:
        print(f"[ERROR] yfinance: {e}")
        return []

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
                print(f"[WARN] Polygon bars attempt {attempt+1} failed, retrying...")
                time.sleep(2)
            else:
                print(f"[WARN] Polygon failed — switching to yfinance")
                return get_spx_bars_yfinance(limit)
    return get_spx_bars_yfinance(limit)

# VIX cache — avoid fetching more than once per minute
_vix_cache = {"value": None, "ts": None}

def get_vix():
    now = datetime.datetime.now(ET)
    if _vix_cache["ts"] and (now - _vix_cache["ts"]).total_seconds() < 60:
        return _vix_cache["value"]
    for attempt in range(3):
        try:
            url = f"https://api.polygon.io/v2/aggs/ticker/I:VIX/range/1/day/2020-01-01/{datetime.date.today().isoformat()}?adjusted=true&sort=desc&limit=1&apiKey={POLYGON_API_KEY}"
            r = requests.get(url, timeout=10)
            data = r.json()
            if data.get("results"):
                val = float(data["results"][0]["c"])
                _vix_cache["value"] = val
                _vix_cache["ts"]    = now
                return val
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                print(f"[WARN] VIX unavailable: {e}")
    return _vix_cache["value"]  # return last cached value if fetch fails

def get_prev_day_levels():
    """Get prior day high and low for key levels"""
    try:
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        # Go back up to 5 days to find last trading day
        for days_back in range(1, 6):
            d = (datetime.date.today() - datetime.timedelta(days=days_back)).isoformat()
            url = (
                f"https://api.polygon.io/v2/aggs/ticker/I:SPX/range/1/day/{d}/{d}"
                f"?adjusted=true&apiKey={POLYGON_API_KEY}"
            )
            r = requests.get(url, timeout=10)
            data = r.json()
            if data.get("results"):
                res = data["results"][0]
                return {
                    "pdh": round(res["h"], 2),
                    "pdl": round(res["l"], 2),
                    "pdc": round(res["c"], 2),
                }
    except Exception as e:
        print(f"[WARN] prev day levels: {e}")
    return None

def get_premarket_levels(bars):
    """Get premarket high and low from bars before 9:30 ET"""
    if not bars:
        return None, None
    pm_bars = [b for b in bars if b.get("t") and
               datetime.datetime.fromtimestamp(b["t"]/1000, ET).time() < datetime.time(9, 30)]
    if not pm_bars:
        return None, None
    pmh = max(b["h"] for b in pm_bars)
    pml = min(b["l"] for b in pm_bars)
    return round(pmh, 2), round(pml, 2)

# ─────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────
def calc_vwap(bars):
    """Calculate VWAP using RTH bars only (9:30 AM ET onwards)"""
    try:
        rth_bars = []
        for b in bars:
            if b.get("t"):
                bar_time = datetime.datetime.fromtimestamp(b["t"]/1000, ET)
                if bar_time.hour > 9 or (bar_time.hour == 9 and bar_time.minute >= 30):
                    rth_bars.append(b)
            else:
                rth_bars.append(b)  # no timestamp — include by default
        if not rth_bars:
            return None
        tp_vol = sum(((b["h"] + b["l"] + b["c"]) / 3) * b.get("v", 0) for b in rth_bars)
        vol    = sum(b.get("v", 0) for b in rth_bars)
        return round(tp_vol / vol, 2) if vol else None
    except:
        return None

def calc_momentum(bars, n=3):
    """Net move over last n bars"""
    if len(bars) < n + 1:
        return 0
    return round(bars[-1]["c"] - bars[-(n+1)]["c"], 2)

def bars_confirming(bars, direction, n=2):
    """Check if last n bars are green (BULL) or red (BEAR)"""
    recent = bars[-n:]
    if direction == "BULL":
        return all(b["c"] >= b["o"] for b in recent)
    else:
        return all(b["c"] <= b["o"] for b in recent)

def expanding_range(bars, n=3):
    """Check if bar ranges are expanding (accelerating move)"""
    if len(bars) < n:
        return False
    ranges = [b["h"] - b["l"] for b in bars[-n:]]
    return ranges[-1] > ranges[0]

def calculate_atr(bars, period=14):
    """Calculate Average True Range over N bars"""
    if len(bars) < period + 1:
        return None
    true_ranges = []
    for i in range(1, len(bars)):
        high  = bars[i]["h"]
        low   = bars[i]["l"]
        prev_close = bars[i-1]["c"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    if len(true_ranges) < period:
        return None
    return round(sum(true_ranges[-period:]) / period, 2)

# ─────────────────────────────────────────
# ECONOMIC CALENDAR
# ─────────────────────────────────────────
HIGH_IMPACT_KEYWORDS = [
    "consumer price index", "cpi", "federal reserve", "fomc", "fed rate",
    "nonfarm payroll", "nfp", "jobs report", "gdp", "pce",
    "producer price index", "ppi", "unemployment", "jobless claims",
    "retail sales", "consumer sentiment", "ism"
]

def get_economic_events():
    today     = datetime.date.today()
    today_str = today.isoformat()
    events    = []

    # Manual events — update each week
    MANUAL_EVENTS = [
        # ("08:30", "CPI — Consumer Price Index"),
    ]

    for time_str, name in MANUAL_EVENTS:
        try:
            h, m = map(int, time_str.split(":"))
            event_dt = ET.localize(datetime.datetime(today.year, today.month, today.day, h, m))
            events.append({"name": name, "time": event_dt})
        except:
            continue

    print(f"[CALENDAR] {len(events)} high-impact events today: {[e['name'] for e in events]}")
    return events

def check_event_blackout(events, window_before=30, window_after=1):
    now = datetime.datetime.now(ET)
    for event in events:
        mins_until = (event["time"] - now).total_seconds() / 60
        mins_since = (now - event["time"]).total_seconds() / 60
        if 0 <= mins_until <= window_before:
            return True, event["name"], round(mins_until), "before"
        if 0 <= mins_since <= window_after:
            return True, event["name"], round(mins_since), "after"
    return False, None, None, None

# ─────────────────────────────────────────
# REGIME FILTER
# ─────────────────────────────────────────
def count_vwap_crosses(bars, vwap):
    """Count how many times price crossed VWAP in bars — high count = chop"""
    crosses = 0
    for i in range(1, len(bars)):
        prev_c = bars[i-1]["c"]
        curr_c = bars[i]["c"]
        if (prev_c < vwap <= curr_c) or (prev_c > vwap >= curr_c):
            crosses += 1
    return crosses

def count_direction_changes(bars):
    """Count bar-to-bar direction reversals — high count = chop even on wide range days"""
    changes = 0
    for i in range(2, len(bars)):
        prev_move = bars[i-1]["c"] - bars[i-2]["c"]
        curr_move = bars[i]["c"] - bars[i-1]["c"]
        if prev_move * curr_move < 0:  # opposite signs = reversal
            changes += 1
    return changes

def compression_detected(bars):
    """
    Detect pre-move compression: recent bars have tighter range than prior bars.
    Compression -> expansion is a high-quality setup signal.
    """
    if len(bars) < 30:
        return False
    recent_ranges = [b["h"] - b["l"] for b in bars[-10:]]
    prior_ranges  = [b["h"] - b["l"] for b in bars[-30:-10]]
    avg_recent = sum(recent_ranges) / len(recent_ranges)
    avg_prior  = sum(prior_ranges) / len(prior_ranges)
    return avg_recent < avg_prior * 0.6  # recent range < 60% of prior = compression

def get_regime(vix, bars, vwap=None, vwap_history=None):
    """
    Classify market regime using VIX + ATR-relative chop detection
    + point-in-time VWAP crosses + direction changes.
    Returns: 'HIGH_VOL', 'ELEVATED', 'NORMAL', 'CHOP'
    """
    # Macro filter (VIX)
    if vix and vix >= 30:
        base_regime = "HIGH_VOL"
    elif vix and vix >= 20:
        base_regime = "ELEVATED"
    else:
        base_regime = "NORMAL"

    if len(bars) >= 20:
        recent    = bars[-20:]
        range_pts = max(b["h"] for b in recent) - min(b["l"] for b in recent)

        # ATR-relative thresholds
        atr      = calculate_atr(bars, period=14)
        is_tight = range_pts < (atr * 2.8) if atr else range_pts < 12
        is_dead  = range_pts < (atr * 1.5) if atr else range_pts < 7

        # Point-in-time VWAP crosses using vwap_history
        vwap_crosses = 0
        if vwap_history and len(vwap_history) >= 20:
            hist_v = vwap_history[-20:]
            for i in range(1, len(recent)):
                prev_cross = (recent[i-1]["c"] < hist_v[i-1] and recent[i]["c"] >= hist_v[i])
                next_cross = (recent[i-1]["c"] > hist_v[i-1] and recent[i]["c"] <= hist_v[i])
                if prev_cross or next_cross:
                    vwap_crosses += 1
        elif vwap:
            vwap_crosses = count_vwap_crosses(recent, vwap)

        # Direction changes
        dir_changes = count_direction_changes(recent)

        # Chop classification
        if is_tight and vwap_crosses >= 4:
            return "CHOP"
        if is_dead:
            return "CHOP"
        if dir_changes >= 8 and (vwap_crosses >= 3 or is_tight):
            return "CHOP"

    return base_regime

# ─────────────────────────────────────────
# TREND DAY DETECTION
# ─────────────────────────────────────────
def detect_trend_day(bars, vwap_history, or_high=None, or_low=None):
    """
    Detect if today is developing into a trend day.
    Requires 30 bars and 30 VWAP history readings (available after ~9:45 ET).
    Returns: 'BULL_TREND', 'BEAR_TREND', or 'NONE'
    """
    if len(bars) < 20 or not vwap_history or len(vwap_history) < 20:
        return "NONE"

    recent = bars[-20:]
    hist_v = vwap_history[-20:]

    # Count bars above/below VWAP
    closes_above = sum(1 for i in range(len(recent)) if recent[i]["c"] > hist_v[i])
    closes_below = sum(1 for i in range(len(recent)) if recent[i]["c"] < hist_v[i])

    # Count VWAP crosses in last 30 bars
    vwap_crosses = 0
    for i in range(1, len(recent)):
        prev_above = recent[i-1]["c"] > hist_v[i-1]
        curr_above = recent[i]["c"] > hist_v[i]
        if prev_above != curr_above:
            vwap_crosses += 1

    last_close = recent[-1]["c"]
    bull_break = or_high is not None and last_close > or_high
    bear_break = or_low  is not None and last_close < or_low

    # Bull trend: price mostly above VWAP, few crosses, OR High broken and holding
    if closes_above >= 16 and vwap_crosses <= 2 and bull_break:
        return "BULL_TREND"

    # Bear trend: price mostly below VWAP, few crosses, OR Low broken and holding
    if closes_below >= 16 and vwap_crosses <= 2 and bear_break:
        return "BEAR_TREND"

    return "NONE"

# ─────────────────────────────────────────
# WEIGHTED SIGNAL SCORING
# ─────────────────────────────────────────
TRIGGER_WEIGHTS = {
    "vwap_at_key_level": 10,
    "vwap_reclaim":       7,
    "vwap_rejection":     7,
    "or_breakout":        9,
    "or_breakdown":       9,
    "pdh_breakout":       8,
    "pdl_breakdown":      8,
    "level_breakout":     7,
    "level_rejection":    5,
    "level_support":      5,
    "momentum_surge":     4,
}

def score_signal(trigger_type, near_key_level=False):
    if near_key_level and "vwap" in trigger_type:
        return TRIGGER_WEIGHTS["vwap_at_key_level"]
    return TRIGGER_WEIGHTS.get(trigger_type, 5)

# ─────────────────────────────────────────
# ADAPTIVE EXITS BY SESSION
# ─────────────────────────────────────────
def get_session(now_et):
    t = now_et.time()
    if t < datetime.time(11, 0):
        return "MORNING"
    elif t < datetime.time(14, 0):
        return "MIDDAY"
    else:
        return "AFTERNOON"

def get_exit_params(session, regime, momentum):
    base_move = max(abs(momentum) * 1.5, 5)
    if session == "MORNING":
        t1_mult, t2_mult, stop_pct = 2.0, 4.0, 50
    elif session == "MIDDAY":
        t1_mult, t2_mult, stop_pct = 1.5, 2.5, 40
    else:
        t1_mult, t2_mult, stop_pct = 1.0, 2.0, 35
    if regime == "HIGH_VOL":
        t1_mult *= 1.3
        t2_mult *= 1.3
    return {"t1_mult": t1_mult, "t2_mult": t2_mult, "stop_pct": stop_pct, "base_move": base_move}

# ─────────────────────────────────────────
# SIGNAL ENGINE — VWAP + LEVELS + MOMENTUM
# ─────────────────────────────────────────
def evaluate_signal(bars, vwap, key_levels, vix=None, last_signal_price=None,
                    last_signal_bias=None, last_vwap_signal_time=None,
                    vwap_history=None, trend_day="NONE"):
    """
    Weighted scoring signal engine with full hardening.
    Score thresholds: CHOP=14, NORMAL=7, ELEVATED=5, HIGH_VOL=4
    Midday adds +2 to threshold.
    """
    if len(bars) < 10 or not vwap:
        return None

    spot     = bars[-1]["c"]
    prev     = bars[-2]["c"]
    momentum = calc_momentum(bars, MOMENTUM_BARS)
    now_et   = datetime.datetime.now(ET)
    session  = get_session(now_et)
    regime   = get_regime(vix, bars, vwap, vwap_history=vwap_history)

    # Rule 6: Hard suppress ALL signals if too extended from VWAP
    vwap_distance = abs(spot - vwap) if vwap else 0
    if vwap_distance > VWAP_MAX_EXTENSION:
        print(f"  → Hard suppress: {vwap_distance:.0f}pts from VWAP (>{VWAP_MAX_EXTENSION})")
        return None

    # Extension threshold — widen in trend direction on trend days (must be before too_extended)
    vwap_break_ext = VWAP_BREAK_EXTENSION  # default 25pts
    if trend_day == "BULL_TREND" or trend_day == "BEAR_TREND":
        vwap_break_ext = 35  # allow continuation further from VWAP on trend days

    too_extended = vwap_distance > vwap_break_ext  # breakout/momentum filter only

    # Rule 3: VWAP flip cooldown
    vwap_flip_ok = True
    if last_vwap_signal_time:
        mins_since_vwap = (now_et - last_vwap_signal_time).total_seconds() / 60
        if mins_since_vwap < VWAP_FLIP_COOLDOWN:
            vwap_flip_ok = False

    # Rule 2: Session score threshold adjustment
    thresholds = {"CHOP": 14, "NORMAL": 7, "ELEVATED": 5, "HIGH_VOL": 4}
    min_score  = thresholds.get(regime, 7)
    if session == "MIDDAY":
        min_score += 2

    # Trend day threshold adjustments
    # Continuation signals easier, countertrend harder
    trend_day_bias = None
    if trend_day == "BULL_TREND":
        trend_day_bias = "BULL"
        print(f"  → BULL TREND DAY: lowering bull threshold, raising bear threshold")
    elif trend_day == "BEAR_TREND":
        trend_day_bias = "BEAR"
        print(f"  → BEAR TREND DAY: lowering bear threshold, raising bull threshold")

    near_any_key_level = any(
        v is not None and abs(spot - v) <= LEVEL_PROXIMITY
        for k, v in key_levels.items() if k != "VWAP"
    )

    is_compressed = compression_detected(bars)

    # Rule 4: Minimum bar range for momentum/breakout signals
    recent_bar_range = bars[-1]["h"] - bars[-1]["l"]
    momentum_range_ok = recent_bar_range >= 2.0

    signals = []

    # 1. VWAP RECLAIM — gated by flip cooldown
    if prev < vwap <= spot and bars_confirming(bars, "BULL", n=2) and momentum > 0:
        if vwap_flip_ok:
            signals.append({"trigger": "VWAP Reclaim", "bias": "BULL",
                            "weight": score_signal("vwap_reclaim", near_any_key_level),
                            "trigger_type": "vwap_reclaim"})

    # 2. VWAP REJECTION — gated by flip cooldown
    if prev > vwap >= spot and bars_confirming(bars, "BEAR", n=2) and momentum < 0:
        if vwap_flip_ok:
            signals.append({"trigger": "VWAP Rejection", "bias": "BEAR",
                            "weight": score_signal("vwap_rejection", near_any_key_level),
                            "trigger_type": "vwap_rejection"})

    # 3. KEY LEVEL REACTIONS
    for name, level in key_levels.items():
        if level is None or name == "VWAP":
            continue
        near = abs(spot - level) <= LEVEL_PROXIMITY

        if "OR" in name:
            btype, dtype = "or_breakout", "or_breakdown"
        elif "Prev Day High" in name:
            btype, dtype = "pdh_breakout", "level_breakout"
        elif "Prev Day Low" in name:
            btype, dtype = "level_breakout", "pdl_breakdown"
        else:
            btype, dtype = "level_breakout", "level_breakout"

        # Breakout — Rule 3(ext) + Rule 7(OR afternoon) + Rule 4+9(momentum range)
        if near and prev <= level < spot and momentum > MOMENTUM_MIN_MOVE:
            is_or = "OR" in name
            if not too_extended and not (is_or and session == "AFTERNOON") and momentum_range_ok:
                signals.append({"trigger": f"Breakout above {name} ({level:,.0f})", "bias": "BULL",
                                "weight": score_signal(btype), "trigger_type": btype})

        # Breakdown — Rule 3(ext) + Rule 7(OR afternoon) + Rule 4+9(momentum range)
        if near and prev >= level > spot and momentum < -MOMENTUM_MIN_MOVE:
            is_or = "OR" in name
            if not too_extended and not (is_or and session == "AFTERNOON") and momentum_range_ok:
                signals.append({"trigger": f"Breakdown below {name} ({level:,.0f})", "bias": "BEAR",
                                "weight": score_signal(dtype), "trigger_type": dtype})

        # Rejection — NOT filtered by extension, slower momentum ok (Rule 9 does not apply)
        if near and spot < level and bars_confirming(bars, "BEAR", n=2) and momentum < -MOMENTUM_MIN_MOVE * 0.5:
            signals.append({"trigger": f"Rejection at {name} ({level:,.0f})", "bias": "BEAR",
                            "weight": score_signal("level_rejection"), "trigger_type": "level_rejection"})

        # Support hold — NOT filtered by extension, slower momentum ok (Rule 9 does not apply)
        if near and spot > level and bars_confirming(bars, "BULL", n=2) and momentum > MOMENTUM_MIN_MOVE * 0.5:
            signals.append({"trigger": f"Support hold at {name} ({level:,.0f})", "bias": "BULL",
                            "weight": score_signal("level_support"), "trigger_type": "level_support"})

    # 4. MOMENTUM SURGE — suppressed if extended or weak bar range
    if abs(momentum) > MOMENTUM_MIN_MOVE * 2 and expanding_range(bars, n=3):
        bias = "BULL" if momentum > 0 else "BEAR"
        if bars_confirming(bars, bias, n=3) and not too_extended and momentum_range_ok:
            signals.append({"trigger": f"Momentum surge ({momentum:+.1f} pts)", "bias": bias,
                            "weight": score_signal("momentum_surge"), "trigger_type": "momentum_surge"})

    if not signals:
        return None

    bull_score = sum(s["weight"] for s in signals if s["bias"] == "BULL")
    bear_score = sum(s["weight"] for s in signals if s["bias"] == "BEAR")

    if bull_score >= bear_score and bull_score > 0:
        dominant_bias, dominant_score = "BULL", bull_score
        dominant_sigs = [s for s in signals if s["bias"] == "BULL"]
    elif bear_score > bull_score:
        dominant_bias, dominant_score = "BEAR", bear_score
        dominant_sigs = [s for s in signals if s["bias"] == "BEAR"]
    else:
        return None

    # Rule 1: Direction-aware signal distance filter
    if last_signal_price and last_signal_bias:
        price_dist = abs(spot - last_signal_price)
        if price_dist < MIN_SIGNAL_DISTANCE and dominant_bias == last_signal_bias:
            print(f"  → Distance filter: same dir, only {price_dist:.1f}pts from last signal")
            return None

    # Trend day threshold adjustment — applied per dominant direction
    if trend_day_bias:
        if dominant_bias == trend_day_bias:
            min_score -= 1  # continuation: easier to fire
        else:
            min_score += 2  # countertrend: much harder to fire

    # Rule 5: Trend persistence — suppress countertrend only (no boost)
    if len(bars) >= 10:
        trend_up   = bars[-1]["c"] > bars[-5]["c"] > bars[-10]["c"]
        trend_down = bars[-1]["c"] < bars[-5]["c"] < bars[-10]["c"]
        if dominant_bias == "BULL" and trend_down:
            dominant_score -= 2
            print(f"  → Countertrend penalty -2 (BULL vs downtrend)")
        if dominant_bias == "BEAR" and trend_up:
            dominant_score -= 2
            print(f"  → Countertrend penalty -2 (BEAR vs uptrend)")

    # Compression boost — only with momentum confirmation
    if is_compressed and abs(momentum) > MOMENTUM_MIN_MOVE:
        dominant_score += 2
        print(f"  → Compression boost +2")

    # Rule 8: Score cap
    dominant_score = min(dominant_score, MAX_SIGNAL_SCORE)

    print(f"  → Score: BULL={bull_score} BEAR={bear_score} final={dominant_score} min={min_score} regime={regime} session={session} trend={trend_day} ext={vwap_distance:.0f}pts")

    if dominant_score < min_score:
        print(f"  → Blocked: {dominant_score} < {min_score}")
        return None

    # Final momentum sanity check — prevent signals on extremely slow drift
    if abs(momentum) < MOMENTUM_MIN_MOVE:
        print(f"  → Final sanity: momentum {momentum:+.1f} below minimum, no signal")
        return None

    quality = "STRONG" if dominant_score >= 14 else "HIGH" if dominant_score >= 7 else "MEDIUM"
    best    = max(dominant_sigs, key=lambda s: s["weight"])

    exit_p = get_exit_params(session, regime, momentum)
    base   = exit_p["base_move"]

    if dominant_bias == "BULL":
        option_type = "CALL"
        strike      = round((spot * 1.002) / 5) * 5
        invalidate  = round(vwap - 5, 2)
        target_1    = round(spot + base * exit_p["t1_mult"], 2)
        target_2    = round(spot + base * exit_p["t2_mult"], 2)
        no_chase    = round(spot + 3, 2)
    else:
        option_type = "PUT"
        strike      = round((spot * 0.998) / 5) * 5
        invalidate  = round(vwap + 5, 2)
        target_1    = round(spot - base * exit_p["t1_mult"], 2)
        target_2    = round(spot - base * exit_p["t2_mult"], 2)
        no_chase    = round(spot - 3, 2)

    return {
        "trigger":         best["trigger"],
        "quality":         quality,
        "score":           dominant_score,
        "regime":          regime,
        "session":         session,
        "bias":            dominant_bias,
        "option_type":     option_type,
        "spot":            round(spot, 2),
        "strike":          strike,
        "vwap":            vwap,
        "momentum":        momentum,
        "invalidate":      invalidate,
        "target_1":        target_1,
        "target_2":        target_2,
        "no_chase":        no_chase,
        "stop_pct":        exit_p["stop_pct"],
        "time_stop":       "3:45 PM ET",
        "vix":             vix,
        "all_signals":     [s["trigger"] for s in dominant_sigs],
        "is_vwap_trigger": best["trigger_type"] in ["vwap_reclaim", "vwap_rejection"],
        "trend_day":       trend_day,
    }



# ─────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown"
        }, timeout=10)
        if r.status_code == 200:
            print(f"[TELEGRAM SENT]")
        else:
            print(f"[TELEGRAM ERROR] {r.status_code}: {r.text}")
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")

def format_signal_message(sig, alert_count):
    t      = datetime.datetime.now(ET).strftime("%I:%M %p ET")
    emoji  = "🟢" if sig["bias"] == "BULL" else "🔴"
    vix_str = f"{sig['vix']:.1f}" if sig.get("vix") else "N/A"

    quality_badges = {
        "STRONG": "⭐⭐ STRONG",
        "HIGH":   "⭐ HIGH",
        "MEDIUM": "MEDIUM"
    }
    badge = quality_badges.get(sig["quality"], sig["quality"])

    regime_labels = {
        "HIGH_VOL":  "🔥 High Vol",
        "ELEVATED":  "⚡ Elevated",
        "NORMAL":    "✅ Normal",
        "CHOP":      "〰️ Chop"
    }
    regime_str  = regime_labels.get(sig["regime"], sig["regime"])
    session_str = sig.get("session", "")
    trend_str   = f" | {'📈 Bull Trend' if sig.get('trend_day') == 'BULL_TREND' else '📉 Bear Trend' if sig.get('trend_day') == 'BEAR_TREND' else ''}" if sig.get("trend_day") != "NONE" else ""

    msg = f"""*SPX 0DTE SIGNAL {emoji} {sig['option_type']}*
━━━━━━━━━━━━━━━━━━━━━━
*Quality:*  {badge} (score: {sig['score']})
*Regime:*   {regime_str} | {session_str}{trend_str}
*Trigger:*  {sig['trigger']}
*Time:*     {t}
━━━━━━━━━━━━━━━━━━━━━━
*Spot:*     {sig['spot']:,.2f}
*Strike:*   {sig['strike']} {sig['option_type']} 0DTE
*VWAP:*     {sig['vwap']:,.2f}
*Momentum:* {sig['momentum']:+.1f} pts
*VIX:*      {vix_str}
━━━━━━━━━━━━━━━━━━━━━━
*T1:* {sig['target_1']:,.2f} | *T2:* {sig['target_2']:,.2f}
*Stop:* -{sig['stop_pct']}% premium | SPX {sig['invalidate']:,.2f}
*No chase:* past {sig['no_chase']:,.2f}
*Exit by:* {sig['time_stop']}
━━━━━━━━━━━━━━━━━━━━━━
Alert {alert_count}/{MAX_ALERTS_PER_DAY}"""

    if len(sig["all_signals"]) > 1:
        extras = "\n".join(f"  + {s}" for s in sig["all_signals"][1:])
        msg += f"\n\n*Also triggered:*\n{extras}"

    return msg

def format_premarket_message(vix, key_levels, events):
    t    = datetime.datetime.now(ET).strftime("%I:%M %p ET")
    date = datetime.date.today().strftime("%A, %B %d, %Y")

    vix_str = f"{vix:.1f}" if vix else "N/A"

    levels_str = ""
    for name, val in key_levels.items():
        if val:
            levels_str += f"\n  {name}: {val:,.2f}"

    events_str = "None"
    if events:
        event_lines = []
        for e in events:
            t_str = e["time"].strftime("%I:%M %p ET")
            event_lines.append(f"  {t_str} — {e['name']}")
        events_str = "\n".join(event_lines)

    return f"""*SPX PRE-MARKET BRIEF*
━━━━━━━━━━━━━━━━━━━━━━
*{date}*
*VIX:* {vix_str}

*Key Levels:*{levels_str}

*High-Impact Events:*
{events_str}
━━━━━━━━━━━━━━━━━━━━━━
Scanning 9:30 AM — 3:30 PM ET
Signals: VWAP reclaim/rejection + key level reactions + momentum surges
Blackout: 30min before / 1min after events"""

# ─────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────
def main():
    print("=" * 50)
    print("  SPX 0DTE Signal Bot v3")
    print("=" * 50)

    alert_count      = 0
    last_signal_time = None
    premarket_sent   = False
    last_date        = None
    today_events     = []
    key_levels       = {}

    # Opening range
    or_high          = None
    or_low           = None
    or_set           = False

    # VIX spike tracking
    last_vix         = None
    last_vix_spike   = None

    # Heartbeat
    last_heartbeat       = None
    last_signal_price    = None
    last_signal_bias      = None
    last_vwap_signal_time  = None
    vwap_history           = []
    trend_day              = "NONE"

    while True:
        now_et = datetime.datetime.now(ET)
        today  = now_et.date()

        # ── Daily reset ──────────────────────────────────────────
        if last_date != today:
            alert_count      = 0
            last_signal_time = None
            premarket_sent   = False
            last_date        = today
            today_events     = []
            key_levels       = {}
            or_high          = None
            or_low           = None
            or_set           = False
            last_vix              = None
            last_vix_spike        = None
            last_heartbeat        = None
            last_signal_price     = None
            last_signal_bias      = None
            last_vwap_signal_time  = None
            vwap_history           = []
            trend_day              = "NONE"
            print(f"\n[{now_et.strftime('%H:%M ET')}] New day — counters reset.")

        # ── Pre-market brief ─────────────────────────────────────
        if is_premarket() and not premarket_sent and now_et.hour >= 6:
            print(f"[{now_et.strftime('%H:%M ET')}] Building pre-market brief...")

            vix = get_vix()
            today_events = get_economic_events()

            # Get key levels
            prev = get_prev_day_levels()
            if prev:
                key_levels = {
                    "Prev Day High":  prev["pdh"],
                    "Prev Day Low":   prev["pdl"],
                    "Prev Day Close": prev["pdc"],
                }

            bars_pm = get_spx_bars(limit=30)
            if bars_pm:
                spot_pm = bars_pm[-1]["c"]
                round_below = (spot_pm // 100) * 100
                round_above = round_below + 100
                key_levels[f"Round {round_below:.0f}"] = round_below
                key_levels[f"Round {round_above:.0f}"] = round_above
                pmh, pml = get_premarket_levels(bars_pm)
                if pmh: key_levels["PM High"] = pmh
                if pml: key_levels["PM Low"]  = pml

            msg = format_premarket_message(vix, key_levels, today_events)
            send_telegram(msg)
            print(f"[TELEGRAM SENT] Pre-market brief")
            premarket_sent = True

        # ── RTH signal loop ──────────────────────────────────────
        if is_market_open() and in_entry_window():

            # Blackout check
            in_blackout, event_name, mins_away, when = check_event_blackout(today_events)
            if in_blackout:
                print(f"[{now_et.strftime('%H:%M ET')}] BLACKOUT — {event_name} ({mins_away}min {when})")
                time.sleep(POLL_INTERVAL_SEC)
                continue

            if alert_count >= MAX_ALERTS_PER_DAY:
                print(f"[{now_et.strftime('%H:%M ET')}] Max alerts hit.")
                time.sleep(POLL_INTERVAL_SEC)
                continue

            bars = get_spx_bars(limit=60)
            if not bars or len(bars) < 10:
                print(f"[{now_et.strftime('%H:%M ET')}] Not enough bars.")
                time.sleep(POLL_INTERVAL_SEC)
                continue

            spot     = bars[-1]["c"]
            vix      = get_vix()
            vwap     = calc_vwap(bars)
            momentum = calc_momentum(bars, MOMENTUM_BARS)

            # Update VWAP in key levels and history
            if vwap:
                key_levels["VWAP"] = vwap
                vwap_history.append(vwap)
                if len(vwap_history) > 60:  # keep last 60 readings
                    vwap_history.pop(0)

            vix_str = f"{vix:.1f}" if vix else "N/A"
            print(f"[{now_et.strftime('%H:%M ET')}] SPX={spot:,.2f} VWAP={vwap} VIX={vix_str} Mom={momentum:+.1f} OR={or_high}/{or_low} Alerts={alert_count}/{MAX_ALERTS_PER_DAY}")

            # ── Opening Range (first 15 min: 9:30-9:45) ──────────
            market_open_time = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
            or_end_time      = now_et.replace(hour=9, minute=45, second=0, microsecond=0)

            if now_et < or_end_time:
                # Still building opening range
                rth_bars = [b for b in bars if b.get("t") and
                            datetime.datetime.fromtimestamp(b["t"]/1000, ET) >= market_open_time]
                if rth_bars:
                    or_high = max(b["h"] for b in rth_bars)
                    or_low  = min(b["l"] for b in rth_bars)
                print(f"  → Building opening range: {or_low} - {or_high}")
                time.sleep(POLL_INTERVAL_SEC)
                continue

            elif not or_set and or_high and or_low:
                # Opening range just locked in
                or_set = True
                key_levels["OR High"] = round(or_high, 2)
                key_levels["OR Low"]  = round(or_low, 2)
                print(f"  → Opening range set: {or_low:.2f} - {or_high:.2f}")
                send_telegram(f"📊 *Opening Range Set*\nHigh: {or_high:,.2f}\nLow: {or_low:,.2f}\nScanning for breakouts...")

            # ── Trend Day Detection (runs every cycle after OR set) ──
            if or_set and trend_day == "NONE":
                trend_day = detect_trend_day(bars, vwap_history, or_high=or_high, or_low=or_low)
                if trend_day != "NONE":
                    emoji = "📈" if trend_day == "BULL_TREND" else "📉"
                    send_telegram(f"{emoji} *Trend Day Detected: {trend_day}*\nSPX={spot:,.2f} | VWAP={vwap}\nContinuation signals prioritized. Fades suppressed.")
                    print(f"  → TREND DAY: {trend_day}")

            # ── VIX Spike Alert ───────────────────────────────────
            if vix and last_vix:
                vix_chg_pct = (vix - last_vix) / last_vix * 100
                if vix_chg_pct >= 8 and last_vix_spike != round(vix, 1):
                    last_vix_spike = round(vix, 1)
                    send_telegram(f"⚠️ *VIX SPIKE ALERT*\nVIX jumped {vix_chg_pct:+.1f}% → {vix:.1f}\nSPX={spot:,.2f}\nExpect volatility — wait for direction before entry")
                    print(f"  → VIX SPIKE: {last_vix:.1f} → {vix:.1f} ({vix_chg_pct:+.1f}%)")
            if vix:
                last_vix = vix

            # ── Hourly Heartbeat ──────────────────────────────────
            if last_heartbeat is None or (now_et - last_heartbeat).total_seconds() >= 3600:
                last_heartbeat = now_et
                send_telegram(f"💓 *Bot Heartbeat* — {now_et.strftime('%I:%M %p ET')}\nSPX={spot:,.2f} | VWAP={vwap} | VIX={vix_str}\nAlerts today: {alert_count}/{MAX_ALERTS_PER_DAY}")
                print(f"  → Heartbeat sent")

            # ── Signal evaluation ─────────────────────────────────
            cooldown_ok = True
            if last_signal_time:
                mins = (now_et - last_signal_time).total_seconds() / 60
                if mins < COOLDOWN_MINUTES:
                    cooldown_ok = False

            if cooldown_ok:
                sig = evaluate_signal(
                    bars, vwap, key_levels, vix=vix,
                    last_signal_price=last_signal_price,
                    last_signal_bias=last_signal_bias,
                    last_vwap_signal_time=last_vwap_signal_time,
                    vwap_history=vwap_history,
                    trend_day=trend_day
                )
                if sig:
                    alert_count          += 1
                    last_signal_time      = now_et
                    last_signal_price     = sig["spot"]
                    last_signal_bias      = sig["bias"]
                    if sig.get("is_vwap_trigger"):
                        last_vwap_signal_time = now_et
                    msg = format_signal_message(sig, alert_count)
                    send_telegram(msg)
                    print(f"  → SIGNAL: {sig['trigger']} | {sig['bias']} | {sig['quality']} (score={sig['score']})")
                else:
                    print(f"  → No signal | VWAP={vwap} Mom={momentum:+.1f}")

        elif is_market_open() and not in_entry_window():
            print(f"[{now_et.strftime('%H:%M ET')}] Outside entry window.")

        elif not is_market_open() and not is_premarket():
            print(f"[{now_et.strftime('%H:%M ET')}] Market closed.")

        time.sleep(POLL_INTERVAL_SEC)

# ─────────────────────────────────────────
# HEALTH CHECK SERVER
# ─────────────────────────────────────────
app = Flask(__name__)

@app.route("/health")
def health():
    return {"status": "ok", "bot": "SPX 0DTE Signal Bot v3"}, 200

@app.route("/")
def index():
    return {"status": "running"}, 200

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

if __name__ == "__main__":
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    print(f"[HEALTH] Health check server started on port {os.environ.get('PORT', 8080)}")
    main()
