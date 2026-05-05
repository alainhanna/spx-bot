import requests
import time
import datetime
import pytz
import os
import threading
import queue
import csv
from flask import Flask

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
POLYGON_API_KEY  = os.environ.get("POLYGON_API_KEY")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

if not POLYGON_API_KEY:
    raise ValueError("POLYGON_API_KEY environment variable not set")
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN environment variable not set")
if not TELEGRAM_CHAT_ID:
    raise ValueError("TELEGRAM_CHAT_ID environment variable not set")

POLL_INTERVAL_SEC  = 15
COOLDOWN_MINUTES   = 2

# Momentum engine thresholds
MOMENTUM_3BAR_MIN            = 3.0
MOMENTUM_5BAR_MIN            = 4.5
MOMENTUM_3BAR_MIN_MIDDAY     = 2.0
MOMENTUM_5BAR_MIN_MIDDAY     = 3.5
MOMENTUM_3BAR_MIN_AFTNOON    = 2.0
MOMENTUM_5BAR_MIN_AFTNOON    = 3.0
MOMENTUM_BAR_RANGE_EXPANSION = 1.2
MOMENTUM_CLOSE_STRENGTH_PCT  = 0.6
MOMENTUM_MAX_OPPOSITE_BARS   = 1
MOMENTUM_MAX_WICK_PCT        = 0.4

# Compression detection config
COMPRESSION_RANGE_RATIO   = 0.60
COMPRESSION_ATR_RATIO     = 0.50
COMPRESSION_LEVEL_WINDOW  = 8.0
COMPRESSION_MIN_RTH_BARS  = 25
COMPRESSION_MAX_BONUS     = 4
BREAKOUT_FOLLOW_THROUGH_MIN = 2.0
BREAKOUT_RETEST_WINDOW      = 8.0
RETEST_LOOKBACK             = 12
BREAKOUT_MIN_MOMENTUM       = 3.0

# Trap engine config
TRAP_MAX_BARS   = 3     # max bars after break for failure to complete
TRAP_BASE_SCORE = 10    # base score — higher conviction than standard breakout

# Shared
MIN_SIGNAL_DISTANCE = 4.0

# Early Trend Continuation Mode — earlier long-only entry for trend/grind days
EARLY_TREND_ENABLED                = True
EARLY_TREND_MIN_RTH_BARS           = 20
EARLY_TREND_MAX_DISTANCE_FROM_VWAP = 18.0
EARLY_TREND_MIN_MOMENTUM           = 2.5
EARLY_TREND_VWAP_SLOPE_MIN         = 0.05
EARLY_TREND_PULLBACK_LOOKBACK      = 8
EARLY_TREND_MIN_CLOSES_ABOVE_VWAP  = 6

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
    s = now.replace(hour=6, minute=0,  second=0, microsecond=0)
    e = now.replace(hour=9, minute=29, second=0, microsecond=0)
    return s <= now <= e

def get_session(now_et):
    t = now_et.time()
    if t < datetime.time(11, 0):
        return "MORNING"
    elif t < datetime.time(14, 0):
        return "MIDDAY"
    else:
        return "AFTERNOON"

# ─────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────
def get_spx_bars(limit=80):
    today = datetime.datetime.now(ET).date().isoformat()
    url = (
        f"https://api.polygon.io/v2/aggs/ticker/I:SPX/range/1/minute/{today}/{today}"
        f"?adjusted=true&sort=desc&limit={limit}&apiKey={POLYGON_API_KEY}"
    )
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 429:
                time.sleep(2 ** (attempt + 1))
                continue
            if r.status_code != 200:
                raise Exception(f"HTTP {r.status_code}")
            data    = r.json()
            results = data.get("results", [])
            if results:
                results = list(reversed(results))
                print(f"[POLYGON] {len(results)} latest bars fetched")
                return results
            else:
                print(f"[WARN] Polygon 0 bars — status={data.get('status')}")
        except Exception as e:
            if attempt < 2:
                print(f"[WARN] Polygon attempt {attempt+1} failed ({e}), retrying...")
                time.sleep(2 ** (attempt + 1))
            else:
                print("[WARN] Polygon bars failed after 3 attempts")
    return []

_vix_cache = {"value": None, "ts": None}

def get_vix():
    now = datetime.datetime.now(ET)
    if _vix_cache["ts"] and (now - _vix_cache["ts"]).total_seconds() < 60:
        return _vix_cache["value"]
    for attempt in range(3):
        try:
            url = f"https://api.polygon.io/v3/snapshot?ticker.any_of=I:VIX&apiKey={POLYGON_API_KEY}"
            r   = requests.get(url, timeout=10)
            if r.status_code == 429:
                time.sleep(2 ** (attempt + 1))
                continue
            if r.status_code == 200:
                results = r.json().get("results", [])
                if results:
                    val = float(results[0].get("session", {}).get("close") or results[0].get("value", 0))
                    if val > 0:
                        _vix_cache["value"] = round(val, 2)
                        _vix_cache["ts"]    = now
                        return _vix_cache["value"]
            r2   = requests.get(f"https://api.polygon.io/v2/aggs/ticker/I:VIX/prev?apiKey={POLYGON_API_KEY}", timeout=10)
            data = r2.json()
            if data.get("results"):
                val = float(data["results"][0]["c"])
                _vix_cache["value"] = round(val, 2)
                _vix_cache["ts"]    = now
                return _vix_cache["value"]
        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
            else:
                print(f"[WARN] VIX unavailable: {e}")
    return _vix_cache["value"]

def get_spx_live_price():
    """
    Fetch live SPX index value from Polygon indices snapshot.
    Used for fast-poll mode (15-second interval when setup is active).
    Endpoint: /v3/snapshot/indices?ticker=I:SPX
    Returns float price or None on failure.
    """
    try:
        url = f"https://api.polygon.io/v3/snapshot/indices?ticker=I:SPX&apiKey={POLYGON_API_KEY}"
        r   = requests.get(url, timeout=5)
        if r.status_code == 200:
            results = r.json().get("results", [])
            if results:
                val = results[0].get("value") or results[0].get("session", {}).get("close")
                if val:
                    return float(val)
    except Exception as e:
        print(f"[WARN] get_spx_live_price failed: {e}")
    return None

def get_prev_day_levels():
    for days_back in range(1, 6):
        try:
            d   = (datetime.date.today() - datetime.timedelta(days=days_back)).isoformat()
            url = (f"https://api.polygon.io/v2/aggs/ticker/I:SPX/range/1/day/{d}/{d}"
                   f"?adjusted=true&apiKey={POLYGON_API_KEY}")
            r   = requests.get(url, timeout=10)
            data = r.json()
            if data.get("results"):
                res = data["results"][0]
                return {"pdh": round(res["h"], 2), "pdl": round(res["l"], 2), "pdc": round(res["c"], 2)}
        except Exception as e:
            print(f"[WARN] prev day levels: {e}")
    return None

# ─────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────
def calc_vwap(bars):
    rth_bars = []
    for b in bars:
        t = b.get("t")
        if t:
            bar_dt = datetime.datetime.fromtimestamp(t / 1000, ET)
            if bar_dt.hour > 9 or (bar_dt.hour == 9 and bar_dt.minute >= 30):
                rth_bars.append(b)
    if not rth_bars:
        print(f"[VWAP] No RTH bars from {len(bars)} total")
        return None
    tp_vol = sum(((b["h"] + b["l"] + b["c"]) / 3) * b.get("v", 1) for b in rth_bars)
    vol    = sum(b.get("v", 1) for b in rth_bars)
    vwap   = round(tp_vol / vol, 2) if vol else None
    print(f"[VWAP] {len(rth_bars)} RTH bars -> {vwap}")
    return vwap

def calc_momentum(bars, n=3):
    if len(bars) < n + 1:
        return 0.0
    return round(bars[-1]["c"] - bars[-(n+1)]["c"], 2)

def calc_atr(bars, period=14):
    if len(bars) < period + 1:
        return 5.0
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i-1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return round(sum(trs[-period:]) / period, 2)

def calc_vwap_slope(vwap_history, n=5):
    if not vwap_history or len(vwap_history) < n:
        return 0.0
    return round(vwap_history[-1] - vwap_history[-n], 3)

def get_regime(vix):
    if vix and vix >= 30:
        return "HIGH_VOL"
    elif vix and vix >= 20:
        return "ELEVATED"
    return "NORMAL"

def build_exit_params(session, regime, atr, bias, spot, vwap):
    if session == "MORNING":
        t1_mult, t2_mult, stop_pct = 1.5, 3.0, 50
    elif session == "MIDDAY":
        t1_mult, t2_mult, stop_pct = 1.0, 2.0, 45
    else:
        t1_mult, t2_mult, stop_pct = 0.8, 1.5, 40
    if regime == "HIGH_VOL":
        t1_mult *= 1.3
        t2_mult *= 1.3
    t1_dist = round(atr * t1_mult, 2)
    t2_dist = round(atr * t2_mult, 2)
    if bias == "BULL":
        option_type = "CALL"
        strike      = round((spot * 1.002) / 5) * 5
        target_1    = round(spot + t1_dist, 2)
        target_2    = round(spot + t2_dist, 2)
        invalidate  = round((vwap - atr * 0.5) if vwap else spot - atr, 2)
        no_chase    = round(spot + 3, 2)
    else:
        option_type = "PUT"
        strike      = round((spot * 0.998) / 5) * 5
        target_1    = round(spot - t1_dist, 2)
        target_2    = round(spot - t2_dist, 2)
        invalidate  = round((vwap + atr * 0.5) if vwap else spot + atr, 2)
        no_chase    = round(spot - 3, 2)
    return {
        "option_type": option_type, "strike": strike,
        "target_1": target_1, "target_2": target_2,
        "invalidate": invalidate, "no_chase": no_chase, "stop_pct": stop_pct,
    }

# ─────────────────────────────────────────
# ECONOMIC CALENDAR
# ─────────────────────────────────────────
def get_economic_events():
    today  = datetime.date.today()
    events = []
    MANUAL_EVENTS = [
        # ("08:30", "CPI"),
        # ("14:00", "FOMC"),
    ]
    for time_str, name in MANUAL_EVENTS:
        try:
            h, m = map(int, time_str.split(":"))
            dt   = ET.localize(datetime.datetime(today.year, today.month, today.day, h, m))
            events.append({"name": name, "time": dt})
        except:
            continue
    print(f"[CALENDAR] {len(events)} events today")
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
# ENGINE 1 — MOMENTUM SIGNAL (PRIMARY)
# ─────────────────────────────────────────
def _close_strength(bar, direction):
    bar_range = bar["h"] - bar["l"]
    if bar_range < 0.5:
        return False
    if direction == "BULL":
        return (bar["c"] - bar["l"]) / bar_range >= MOMENTUM_CLOSE_STRENGTH_PCT
    else:
        return (bar["h"] - bar["c"]) / bar_range >= MOMENTUM_CLOSE_STRENGTH_PCT

def _wick_ok(bar, direction):
    bar_range = bar["h"] - bar["l"]
    if bar_range < 0.5:
        return True
    if direction == "BULL":
        return (bar["h"] - bar["c"]) / bar_range <= MOMENTUM_MAX_WICK_PCT
    else:
        return (bar["c"] - bar["l"]) / bar_range <= MOMENTUM_MAX_WICK_PCT

def calc_compression_score(bars, atr, key_levels, now_et):
    """
    Detects pre-move compression (coiling) and returns a bonus score (0-4).
    Range contraction is mandatory gate. Amplifier only.
    """
    if now_et.time() < datetime.time(9, 45):
        return 0, []
    rth_bars = [b for b in bars if b.get("t") and
                datetime.datetime.fromtimestamp(b["t"] / 1000, ET).time() >= datetime.time(9, 30)]
    if len(rth_bars) < COMPRESSION_MIN_RTH_BARS:
        return 0, []
    latest       = bars[-1]
    latest_range = latest["h"] - latest["l"]
    c_score      = 0
    c_reasons    = []
    if len(bars) >= 20:
        avg_recent = sum(b["h"] - b["l"] for b in bars[-5:]) / 5
        avg_prior  = sum(b["h"] - b["l"] for b in bars[-20:-5]) / 15
        range_contraction = avg_prior > 0 and avg_recent < avg_prior * COMPRESSION_RANGE_RATIO
    else:
        range_contraction = False
    if range_contraction:
        c_score += 2
        c_reasons.append("range contraction")
    else:
        return 0, []
    if atr > 0 and latest_range < atr * COMPRESSION_ATR_RATIO:
        c_score += 1
        c_reasons.append("ATR contraction")
    spot = latest["c"]
    coiling_near_level = any(
        v is not None and abs(spot - v) <= COMPRESSION_LEVEL_WINDOW
        for k, v in key_levels.items()
    )
    if coiling_near_level:
        c_score += 2
        c_reasons.append("near key level")
    if len(bars) >= 5:
        last5       = bars[-5:]
        moves       = [last5[i]["c"] - last5[i-1]["c"] for i in range(1, len(last5))]
        dir_changes = sum(1 for i in range(1, len(moves)) if moves[i] * moves[i-1] < 0)
        net_move_5  = abs(last5[-1]["c"] - last5[0]["c"])
        if dir_changes >= 3 and net_move_5 < atr * 0.35:
            c_score += 1
            c_reasons.append(f"coiling ({dir_changes} dir changes)")
    if not coiling_near_level and c_score > 3:
        c_score = 3
    c_score = min(c_score, COMPRESSION_MAX_BONUS)
    return c_score, c_reasons

def evaluate_momentum_signal(bars, vwap, vwap_history, vix, session, key_levels=None, now_et=None):
    """Primary engine. Fires on impulse price moves regardless of key levels."""
    if len(bars) < 6:
        return None
    spot   = bars[-1]["c"]
    mom3   = calc_momentum(bars, 3)
    mom5   = calc_momentum(bars, 5)
    atr    = calc_atr(bars)
    regime = get_regime(vix)
    if session == "MORNING":
        min3, min5, min_score = MOMENTUM_3BAR_MIN, MOMENTUM_5BAR_MIN, 7
    elif session == "MIDDAY":
        min3, min5, min_score = MOMENTUM_3BAR_MIN_MIDDAY, MOMENTUM_5BAR_MIN_MIDDAY, 6
    else:
        min3, min5, min_score = MOMENTUM_3BAR_MIN_AFTNOON, MOMENTUM_5BAR_MIN_AFTNOON, 5
    momentum_volatility_floor = atr * 0.35
    if mom3 >= max(min3, momentum_volatility_floor):
        direction = "BULL"
    elif mom3 <= -max(min3, momentum_volatility_floor):
        direction = "BEAR"
    else:
        return None
    recent5 = bars[-5:]
    recent3 = bars[-3:]
    latest  = bars[-1]
    score   = 0
    reasons = []
    score += 3
    reasons.append(f"3-bar {mom3:+.1f}pts")
    if (direction == "BULL" and mom5 >= min5) or (direction == "BEAR" and mom5 <= -min5):
        score += 2
        reasons.append(f"5-bar {mom5:+.1f}pts")
    latest_range = latest["h"] - latest["l"]
    avg_range    = sum(b["h"] - b["l"] for b in bars[-5:-1]) / 4 if len(bars) >= 5 else latest_range
    if latest_range < atr * 0.20:
        print(f"  [MOM] Rejected: bar range {latest_range:.1f} < ATR*0.20 ({atr * 0.20:.1f})")
        return None
    if avg_range > 0 and latest_range >= avg_range * MOMENTUM_BAR_RANGE_EXPANSION:
        score += 2
        reasons.append("range expansion")
    strong_closes = sum(1 for b in recent3 if _close_strength(b, direction))
    if strong_closes >= 2:
        score += 2
        reasons.append(f"{strong_closes}/3 strong closes")
    elif strong_closes == 1:
        score += 1
    above_vwap = vwap and spot > vwap
    below_vwap = vwap and spot < vwap
    if (direction == "BULL" and above_vwap) or (direction == "BEAR" and below_vwap):
        score += 2
        reasons.append("VWAP aligned")
    elif vwap is None:
        score += 1
    vwap_slope = calc_vwap_slope(vwap_history, n=5)
    if (direction == "BULL" and vwap_slope > 0.15) or (direction == "BEAR" and vwap_slope < -0.15):
        score += 2
        reasons.append(f"VWAP slope {vwap_slope:+.2f}")
    if direction == "BULL":
        opp = sum(1 for b in recent5 if b["c"] < b["o"])
    else:
        opp = sum(1 for b in recent5 if b["c"] > b["o"])
    if opp <= MOMENTUM_MAX_OPPOSITE_BARS:
        score += 1
        reasons.append(f"clean ({opp} opp bars)")
    if _wick_ok(latest, direction):
        score += 1
        reasons.append("wick ok")
    if len(bars) >= 7:
        prior_mom3 = bars[-4]["c"] - bars[-7]["c"]
        if (direction == "BULL" and mom3 > prior_mom3 > 0) or (direction == "BEAR" and mom3 < prior_mom3 < 0):
            score += 2
            reasons.append("accelerating")
    # Compression bonus + vol expansion (gated: expansion only fires after confirmed compression)
    compression_present = False
    if key_levels and now_et:
        c_bonus, c_reasons = calc_compression_score(bars, atr, key_levels, now_et)
        if c_bonus > 0:
            compression_present = True
            score += c_bonus
            reasons.append(f"compression +{c_bonus} ({', '.join(c_reasons)})")
            print(f"  [MOM] Compression bonus +{c_bonus}: {c_reasons}")

    # Vol expansion only fires after genuine compression — not random spikes
    if compression_present and len(bars) >= 20:
        recent_avg_range = sum(b["h"] - b["l"] for b in bars[-20:-5]) / len(bars[-20:-5])
        if latest_range > recent_avg_range * 1.4:
            score += 2
            reasons.append("vol expansion")
    print(f"  [MOM] dir={direction} score={score}/{min_score} | {reasons}")
    if score < min_score:
        return None
    quality = "STRONG" if score >= 13 else "HIGH" if score >= 9 else "MEDIUM"
    exits   = build_exit_params(session, regime, atr, direction, spot, vwap)
    return {
        "signal_type": "MOMENTUM",
        "trigger":     f"Momentum {direction} {mom3:+.1f}pts | {', '.join(reasons[:3])}",
        "bias":        direction,
        "score":       score,
        "quality":     quality,
        "regime":      regime,
        "session":     session,
        "spot":        round(spot, 2),
        "vwap":        vwap,
        "momentum":    mom3,
        "atr":         atr,
        "vix":         vix,
        "time_stop":   "3:45 PM ET",
        "all_candidates": [],
        **exits,
    }

# ─────────────────────────────────────────
# ENGINE 2 — BREAKOUT SIGNAL (SECONDARY)
# ─────────────────────────────────────────
def _bars_above(bars, level, n):
    if len(bars) < n:
        return False
    return all(b["c"] > level for b in bars[-n:])

def _bars_below(bars, level, n):
    if len(bars) < n:
        return False
    return all(b["c"] < level for b in bars[-n:])

def evaluate_breakout_signal(bars, key_levels, vwap, vix, session, or_set):
    """Secondary engine. Fires on confirmed key level breaks and retests."""
    if len(bars) < 5 or not key_levels:
        return None
    spot       = bars[-1]["c"]
    atr        = calc_atr(bars)
    regime     = get_regime(vix)
    above_vwap = vwap and spot > vwap
    below_vwap = vwap and spot < vwap
    mom3       = calc_momentum(bars, 3)
    if session == "MORNING":
        min_mom, confirm = BREAKOUT_MIN_MOMENTUM, 1
    elif session == "MIDDAY":
        min_mom, confirm = 2.5, 2
    else:
        min_mom, confirm = 2.0, 2
    candidates = []
    follow     = BREAKOUT_FOLLOW_THROUGH_MIN
    for level_name, level in key_levels.items():
        if level is None:
            continue
        if "OR" in level_name and not or_set:
            continue
        if level_name == "VWAP":
            continue
        if spot > level + follow and _bars_above(bars, level, confirm) and mom3 >= min_mom:
            score = 7
            if above_vwap:                        score += 2
            if "OR" in level_name:                score += 2
            if "Prev Day High" in level_name:     score += 2
            if "PM" in level_name:                score += 1
            if "Round" in level_name or level_name.startswith("R"): score += 1
            candidates.append({
                "bias": "BULL", "signal_type": "BREAKOUT",
                "trigger": f"Breakout above {level_name} ({level:,.0f})",
                "level": level, "level_name": level_name, "score": score,
            })
            print(f"  [BRK] BULL {level_name} score={score}")
        elif level < spot <= level + BREAKOUT_RETEST_WINDOW:
            was_above = any(b["c"] > level + follow for b in bars[-RETEST_LOOKBACK:-3])
            if was_above and mom3 >= 0 and above_vwap:
                candidates.append({
                    "bias": "BULL", "signal_type": "RETEST",
                    "trigger": f"Retest hold {level_name} ({level:,.0f})",
                    "level": level, "level_name": level_name, "score": 7,
                })
        if spot < level - follow and _bars_below(bars, level, confirm) and mom3 <= -min_mom:
            score = 7
            if below_vwap:                        score += 2
            if "OR" in level_name:                score += 2
            if "Prev Day Low" in level_name:      score += 2
            if "PM" in level_name:                score += 1
            if "Round" in level_name or level_name.startswith("R"): score += 1
            candidates.append({
                "bias": "BEAR", "signal_type": "BREAKDOWN",
                "trigger": f"Breakdown below {level_name} ({level:,.0f})",
                "level": level, "level_name": level_name, "score": score,
            })
            print(f"  [BRK] BEAR {level_name} score={score}")
        elif level - BREAKOUT_RETEST_WINDOW <= spot < level:
            was_below = any(b["c"] < level - follow for b in bars[-RETEST_LOOKBACK:-3])
            if was_below and mom3 <= 0 and below_vwap:
                candidates.append({
                    "bias": "BEAR", "signal_type": "RETEST",
                    "trigger": f"Retest fail {level_name} ({level:,.0f})",
                    "level": level, "level_name": level_name, "score": 7,
                })
    if vwap and len(bars) >= 2:
        prev = bars[-2]["c"]
        if prev < vwap <= spot and mom3 >= min_mom and _bars_above(bars, vwap, confirm):
            score = 8 + (2 if above_vwap else 0)
            candidates.append({
                "bias": "BULL", "signal_type": "BREAKOUT",
                "trigger": f"VWAP Reclaim ({vwap:,.2f})",
                "level": vwap, "level_name": "VWAP", "score": score,
            })
        elif prev > vwap >= spot and mom3 <= -min_mom and _bars_below(bars, vwap, confirm):
            score = 8 + (2 if below_vwap else 0)
            candidates.append({
                "bias": "BEAR", "signal_type": "BREAKDOWN",
                "trigger": f"VWAP Rejection ({vwap:,.2f})",
                "level": vwap, "level_name": "VWAP", "score": score,
            })
    if not candidates:
        return None
    type_rank = {"BREAKOUT": 2, "BREAKDOWN": 2, "RETEST": 1}
    candidates.sort(key=lambda x: (x["score"], type_rank.get(x["signal_type"], 0)), reverse=True)
    best   = candidates[0]
    extras = [c["trigger"] for c in candidates[1:4]]
    quality = "STRONG" if best["score"] >= 11 else "HIGH" if best["score"] >= 9 else "MEDIUM"
    exits   = build_exit_params(session, regime, atr, best["bias"], spot, vwap)
    return {
        "signal_type":    best["signal_type"],
        "trigger":        best["trigger"],
        "bias":           best["bias"],
        "score":          best["score"],
        "quality":        quality,
        "regime":         regime,
        "session":        session,
        "spot":           round(spot, 2),
        "vwap":           vwap,
        "momentum":       mom3,
        "atr":            atr,
        "vix":            vix,
        "level":          best.get("level"),
        "time_stop":      "3:45 PM ET",
        "all_candidates": extras,
        **exits,
    }

# ─────────────────────────────────────────
# ENGINE 3 — TRAP / FAILED BREAKOUT (TERTIARY)
# ─────────────────────────────────────────
def evaluate_trap_signal(bars, key_levels, vwap, vix, session, or_set):
    """
    Tertiary engine. Detects failed breakouts / trap reversals.

    Conditions for a BEAR trap (failed bull breakout → PUT):
      1. Within TRAP_MAX_BARS ago, price broke above a structural level
         by at least TRAP_MIN_RECROSS = max(2.0, atr * 0.25)
      2. Current bar has closed back below that level by at least TRAP_MIN_RECROSS
      3. Current 3-bar momentum is negative (flipped)
      4. Failure bar range >= atr * 0.20 (not a doji)

    Symmetrical for BULL trap (failed bear breakdown → CALL).

    VWAP is excluded as a trap level (handled by breakout engine).
    OR levels excluded until or_set=True.

    Scoring:
      Base: TRAP_BASE_SCORE (10)
      +2  VWAP now on correct side (confirms failure direction)
      +2  OR level involved
      +2  PDH/PDL involved
      +1  momentum accelerating in failure direction
      Soft penalty -2 if trap is against dominant structure
        (bearish trap but price still well above VWAP: may be normal pullback)
    """
    if len(bars) < TRAP_MAX_BARS + 2 or not key_levels:
        return None

    spot     = bars[-1]["c"]
    atr      = calc_atr(bars)
    regime   = get_regime(vix)
    mom3     = calc_momentum(bars, 3)
    above_vwap = vwap and spot > vwap
    below_vwap = vwap and spot < vwap

    # ATR-scaled recross threshold
    trap_min_recross = max(2.0, atr * 0.25)

    # Failure bar range check
    latest_range = bars[-1]["h"] - bars[-1]["l"]
    if latest_range < atr * 0.20:
        return None

    # Session-aware momentum minimum
    if session == "MORNING":
        min_mom = BREAKOUT_MIN_MOMENTUM
    elif session == "MIDDAY":
        min_mom = 2.5
    else:
        min_mom = 2.0

    candidates = []

    for level_name, level in key_levels.items():
        if level is None:
            continue
        if level_name == "VWAP":
            continue  # VWAP traps handled by breakout engine
        if "OR" in level_name and not or_set:
            continue

        # Only look at bars immediately before current — breakout must be recent
        recent_break_window = bars[-3:-1]

        # ── BEAR TRAP: broke above level, failed back below ──────────────────
        was_above = any(b["c"] > level + trap_min_recross for b in recent_break_window)
        # Current bar closed back below level - recross threshold
        now_below = spot < level - trap_min_recross
        # Momentum flipped bearish
        if was_above and now_below and mom3 <= -min_mom:
            score = TRAP_BASE_SCORE
            # VWAP now below (confirms bears in control)
            if below_vwap:
                score += 2
            if "OR" in level_name:
                score += 2
            if "Prev Day High" in level_name:
                score += 2
            if "PM" in level_name:
                score += 1
            # Acceleration in failure direction
            if len(bars) >= 7:
                prior_mom3 = bars[-4]["c"] - bars[-7]["c"]
                if mom3 < prior_mom3 < 0:
                    score += 1
            # Soft penalty: still above VWAP by meaningful distance — may be normal pullback
            if above_vwap and vwap and abs(spot - vwap) > atr * 0.5:
                score -= 2
                print(f"  [TRAP] Bear trap penalty: still {abs(spot - vwap):.1f}pts above VWAP")
            if score >= 10:
                candidates.append({
                    "bias": "BEAR", "signal_type": "TRAP",
                    "trigger": f"Failed breakout above {level_name} ({level:,.0f}) — trapped longs",
                    "level": level, "level_name": level_name, "score": score,
                })
                print(f"  [TRAP] BEAR trap {level_name} score={score} mom={mom3:+.1f}")

        # ── BULL TRAP: broke below level, failed back above ──────────────────
        was_below = any(b["c"] < level - trap_min_recross for b in recent_break_window)
        now_above = spot > level + trap_min_recross
        if was_below and now_above and mom3 >= min_mom:
            score = TRAP_BASE_SCORE
            if above_vwap:
                score += 2
            if "OR" in level_name:
                score += 2
            if "Prev Day Low" in level_name:
                score += 2
            if "PM" in level_name:
                score += 1
            if len(bars) >= 7:
                prior_mom3 = bars[-4]["c"] - bars[-7]["c"]
                if mom3 > prior_mom3 > 0:
                    score += 1
            # Soft penalty: still below VWAP — may be normal bounce
            if below_vwap and vwap and abs(spot - vwap) > atr * 0.5:
                score -= 2
                print(f"  [TRAP] Bull trap penalty: still {abs(spot - vwap):.1f}pts below VWAP")
            if score >= 10:
                candidates.append({
                    "bias": "BULL", "signal_type": "TRAP",
                    "trigger": f"Failed breakdown below {level_name} ({level:,.0f}) — trapped shorts",
                    "level": level, "level_name": level_name, "score": score,
                })
                print(f"  [TRAP] BULL trap {level_name} score={score} mom={mom3:+.1f}")

    if not candidates:
        return None

    candidates.sort(key=lambda x: x["score"], reverse=True)
    best    = candidates[0]
    quality = "STRONG" if best["score"] >= 12 else "HIGH" if best["score"] >= 10 else "MEDIUM"
    exits   = build_exit_params(session, regime, atr, best["bias"], spot, vwap)

    return {
        "signal_type":    best["signal_type"],
        "trigger":        best["trigger"],
        "bias":           best["bias"],
        "score":          best["score"],
        "quality":        quality,
        "regime":         regime,
        "session":        session,
        "spot":           round(spot, 2),
        "vwap":           vwap,
        "momentum":       mom3,
        "atr":            atr,
        "vix":            vix,
        "level":          best.get("level"),
        "level_name":     best.get("level_name", ""),
        "time_stop":      "3:45 PM ET",
        "all_candidates": [c["trigger"] for c in candidates[1:4]],
        **exits,
    }

# ─────────────────────────────────────────
# ENGINE 4 — TREND GRIND / VWAP WALK (QUATERNARY)
# ─────────────────────────────────────────
def evaluate_trend_grind_signal(bars, vwap, vwap_history, vix, session, now_et):
    """
    Detects slow VWAP-walk trend days missed by the momentum engine.
    Fires when price is steadily grinding above (BULL) or below (BEAR) VWAP
    with consistent structure — not a sharp impulse, but a persistent directional walk.

    Conditions (BULL):
      - >= 20 completed RTH bars
      - Time >= 9:45 ET (stable bar history)
      - Price above VWAP for >= 15 of last 20 bars
      - VWAP slope positive over last 5 readings
      - 20-bar net move >= 15pts upward
      - Higher highs AND higher lows over last 10 bars
      - Spot not extended > 2 ATR from VWAP (not chasing)
      - No large rejection wick on latest bar (wick < 40% of range)
      - NOT in first 15 minutes (9:30–9:45)

    Scoring (max ~9):
      Base: 6
      +1  VWAP slope > 0.5 (strong slope)
      +1  20-bar net move >= 25pts (strong grind)
      +1  spot within 0.5 ATR of VWAP (tight walk, not extended)

    Respects existing cooldown and distance filters (applied in evaluate_signal).
    Signal label: TREND_GRIND
    """
    if not vwap or not vwap_history:
        return None

    # Time guard — no early session
    if now_et.time() < datetime.time(9, 45):
        return None

    # Need enough RTH bars
    rth_bars = [b for b in bars if b.get("t") and
                datetime.datetime.fromtimestamp(b["t"] / 1000, ET).time() >= datetime.time(9, 30)]
    if len(rth_bars) < 20:
        return None

    spot    = bars[-1]["c"]
    atr     = calc_atr(bars)
    regime  = get_regime(vix)
    latest  = bars[-1]

    recent20 = rth_bars[-20:]

    # VWAP alignment count over last 20 bars
    # Use vwap_history if long enough, else use current vwap as proxy
    if len(vwap_history) >= 20:
        hist20 = vwap_history[-20:]
        bars_above_vwap = sum(1 for i, b in enumerate(recent20) if b["c"] > hist20[i])
        bars_below_vwap = sum(1 for i, b in enumerate(recent20) if b["c"] < hist20[i])
    else:
        bars_above_vwap = sum(1 for b in recent20 if b["c"] > vwap)
        bars_below_vwap = sum(1 for b in recent20 if b["c"] < vwap)

    # Determine candidate direction
    if bars_above_vwap >= 12:
        direction = "BULL"
    elif bars_below_vwap >= 12:
        direction = "BEAR"
    else:
        return None

    # VWAP slope
    vwap_slope = calc_vwap_slope(vwap_history, n=5)
    if direction == "BULL" and vwap_slope < -0.05:
        print(f"  [GRIND] BULL blocked: VWAP slope {vwap_slope:+.3f} too negative")
        return None
    if direction == "BEAR" and vwap_slope > 0.05:
        print(f"  [GRIND] BEAR blocked: VWAP slope {vwap_slope:+.3f} too positive")
        return None

    # 20-bar net move threshold
    net_move = recent20[-1]["c"] - recent20[0]["c"]
    if direction == "BULL" and net_move < 15.0:
        print(f"  [GRIND] BULL blocked: 20-bar net move {net_move:+.1f} < 15pts")
        return None
    if direction == "BEAR" and net_move > -15.0:
        print(f"  [GRIND] BEAR blocked: 20-bar net move {net_move:+.1f} > -15pts")
        return None

    # Directional bias over last 10 bars — simpler and more reliable than HH/HL intraday
    last10 = rth_bars[-10:]
    net10  = last10[-1]["c"] - last10[0]["c"]
    if direction == "BULL" and net10 <= 0:
        print(f"  [GRIND] BULL blocked: 10-bar net {net10:+.1f} not positive")
        return None
    if direction == "BEAR" and net10 >= 0:
        print(f"  [GRIND] BEAR blocked: 10-bar net {net10:+.1f} not negative")
        return None

    # Extension check — not chasing
    vwap_dist = abs(spot - vwap)
    if vwap_dist > atr * 2.0:
        print(f"  [GRIND] Blocked: too extended {vwap_dist:.1f}pts from VWAP (>{atr * 2.0:.1f})")
        return None

    # Wick filter on latest bar
    bar_range = latest["h"] - latest["l"]
    if bar_range > 0.5:
        if direction == "BULL":
            upper_wick = latest["h"] - latest["c"]
            if upper_wick / bar_range > 0.40:
                print(f"  [GRIND] BULL blocked: rejection wick {upper_wick/bar_range:.0%}")
                return None
        else:
            lower_wick = latest["c"] - latest["l"]
            if lower_wick / bar_range > 0.40:
                print(f"  [GRIND] BEAR blocked: rejection wick {lower_wick/bar_range:.0%}")
                return None

    # Scoring
    mom3  = calc_momentum(bars, 3)
    score = 6  # base
    if (direction == "BULL" and vwap_slope > 0.5) or (direction == "BEAR" and vwap_slope < -0.5):
        score += 1
    if abs(net_move) >= 25.0:
        score += 1
    if vwap_dist <= atr * 0.5:
        score += 1  # tight walk — high quality grind
    # Short-term momentum alignment bonus — avoid entering stalling grinds
    if (direction == "BULL" and mom3 > 0) or (direction == "BEAR" and mom3 < 0):
        score += 1

    print(f"  [GRIND] dir={direction} score={score} net={net_move:+.1f}pts slope={vwap_slope:+.3f} mom3={mom3:+.1f} aligned={bars_above_vwap if direction == 'BULL' else bars_below_vwap}/20")

    quality = "HIGH" if score >= 8 else "MEDIUM"
    exits   = build_exit_params(session, regime, atr, direction, spot, vwap)

    return {
        "signal_type": "TREND_GRIND",
        "trigger":     f"VWAP walk {direction} | {bars_above_vwap if direction == 'BULL' else bars_below_vwap}/20 bars aligned | net {net_move:+.1f}pts",
        "bias":        direction,
        "score":       score,
        "quality":     quality,
        "regime":      regime,
        "session":     session,
        "spot":        round(spot, 2),
        "vwap":        vwap,
        "momentum":    mom3,
        "atr":         atr,
        "vix":         vix,
        "level":       None,
        "level_name":  "",
        "time_stop":   "3:45 PM ET",
        "all_candidates": [],
        **exits,
    }

# ─────────────────────────────────────────
# ENGINE 5 — VWAP ACCEPTANCE (TWO-TIER)
# ─────────────────────────────────────────
def evaluate_vwap_acceptance_signal(bars, vwap, vwap_history, vix, session, now_et, or_high=None, or_low=None):
    """
    Two-tier early regime shift detector.

    EARLY tier (VWAP_ACCEPTANCE_EARLY):
      - 4 of last 6 bars above/below VWAP
      - 6-bar net move >= 3pts in direction
      - VWAP slope not strongly opposing (>= -0.05 BULL / <= +0.05 BEAR)
      - Spot within 1.2 ATR of VWAP
      - No rejection wick > 40%
      - Base score: 4 (+1 slope aligned, +1 mom3 aligned, +1 OR midpoint)

    CONFIRMED tier (VWAP_ACCEPTANCE):
      - 5 of last 7 bars above/below VWAP
      - 7-bar net move >= 5pts in direction
      - Same slope/extension/wick filters
      - Base score: 5 (+1 slope aligned, +1 OR midpoint)

    Early tier fires first if it qualifies. Confirmed tier only fires if early
    conditions are NOT met (avoids double-firing on same setup).
    Both respect cooldown and distance filters in evaluate_signal.
    """
    if not vwap:
        return None
    if now_et.time() < datetime.time(9, 45):
        return None

    rth_bars = [b for b in bars if b.get("t") and
                datetime.datetime.fromtimestamp(b["t"] / 1000, ET).time() >= datetime.time(9, 30)]

    spot   = bars[-1]["c"]
    atr    = calc_atr(bars)
    regime = get_regime(vix)
    latest = bars[-1]
    mom3   = calc_momentum(bars, 3)

    vwap_slope = calc_vwap_slope(vwap_history, n=5)
    vwap_dist  = abs(spot - vwap)
    bar_range  = latest["h"] - latest["l"]

    or_mid = (or_high + or_low) / 2 if (or_high is not None and or_low is not None) else None

    def wick_rejected(direction):
        if bar_range < 0.5:
            return False
        if direction == "BULL":
            return (latest["h"] - latest["c"]) / bar_range > 0.40
        else:
            return (latest["c"] - latest["l"]) / bar_range > 0.40

    # ── EARLY TIER ───────────────────────────────────────────────────────────
    if len(rth_bars) >= 6:
        last6 = rth_bars[-6:]
        if len(vwap_history) >= 6:
            hist6 = vwap_history[-6:]
            early_above = sum(1 for i, b in enumerate(last6) if b["c"] > hist6[i])
            early_below = sum(1 for i, b in enumerate(last6) if b["c"] < hist6[i])
        else:
            early_above = sum(1 for b in last6 if b["c"] > vwap)
            early_below = sum(1 for b in last6 if b["c"] < vwap)

        net6 = last6[-1]["c"] - last6[0]["c"]

        if early_above >= 4:
            early_dir = "BULL"
        elif early_below >= 4:
            early_dir = "BEAR"
        else:
            early_dir = None

        if early_dir:
            slope_ok  = (early_dir == "BULL" and vwap_slope >= -0.05) or (early_dir == "BEAR" and vwap_slope <= 0.05)
            net_ok    = (early_dir == "BULL" and net6 >= 3.0) or (early_dir == "BEAR" and net6 <= -3.0)
            ext_ok    = vwap_dist <= atr * 1.2
            wick_ok   = not wick_rejected(early_dir)

            if slope_ok and net_ok and ext_ok and wick_ok:
                score = 4
                if (early_dir == "BULL" and vwap_slope > 0.20) or (early_dir == "BEAR" and vwap_slope < -0.20):
                    score += 1
                if (early_dir == "BULL" and mom3 > 0) or (early_dir == "BEAR" and mom3 < 0):
                    score += 1
                if or_mid is not None:
                    if (early_dir == "BULL" and spot > or_mid) or (early_dir == "BEAR" and spot < or_mid):
                        score += 1

                aligned = early_above if early_dir == "BULL" else early_below
                print(f"  [EARLY] dir={early_dir} score={score} net6={net6:+.1f}pts slope={vwap_slope:+.3f} mom3={mom3:+.1f} aligned={aligned}/6")

                exits = build_exit_params(session, regime, atr, early_dir, spot, vwap)
                return {
                    "signal_type": "VWAP_ACCEPTANCE_EARLY",
                    "trigger":     f"VWAP early acceptance {early_dir} | {aligned}/6 bars aligned | net {net6:+.1f}pts",
                    "bias":        early_dir,
                    "score":       score,
                    "quality":     "MEDIUM",
                    "regime":      regime,
                    "session":     session,
                    "spot":        round(spot, 2),
                    "vwap":        vwap,
                    "momentum":    mom3,
                    "atr":         atr,
                    "vix":         vix,
                    "level":       None,
                    "level_name":  "",
                    "time_stop":   "3:45 PM ET",
                    "all_candidates": [],
                    **exits,
                }

    # ── CONFIRMED TIER ───────────────────────────────────────────────────────
    if len(rth_bars) < 7:
        return None

    last7 = rth_bars[-7:]
    if len(vwap_history) >= 7:
        hist7 = vwap_history[-7:]
        bars_above = sum(1 for i, b in enumerate(last7) if b["c"] > hist7[i])
        bars_below = sum(1 for i, b in enumerate(last7) if b["c"] < hist7[i])
    else:
        bars_above = sum(1 for b in last7 if b["c"] > vwap)
        bars_below = sum(1 for b in last7 if b["c"] < vwap)

    if bars_above >= 5:
        direction = "BULL"
    elif bars_below >= 5:
        direction = "BEAR"
    else:
        return None

    slope_ok = (direction == "BULL" and vwap_slope >= -0.05) or (direction == "BEAR" and vwap_slope <= 0.05)
    if not slope_ok:
        print(f"  [ACCEPT] {direction} blocked: slope {vwap_slope:+.3f} opposing")
        return None

    net7 = last7[-1]["c"] - last7[0]["c"]
    if (direction == "BULL" and net7 < 5.0) or (direction == "BEAR" and net7 > -5.0):
        print(f"  [ACCEPT] {direction} blocked: 7-bar net {net7:+.1f}")
        return None

    if vwap_dist > atr * 1.5:
        print(f"  [ACCEPT] Blocked: too extended {vwap_dist:.1f}pts from VWAP")
        return None

    if wick_rejected(direction):
        print(f"  [ACCEPT] {direction} blocked: rejection wick")
        return None

    score = 5
    if (direction == "BULL" and vwap_slope > 0.3) or (direction == "BEAR" and vwap_slope < -0.3):
        score += 1
    if or_mid is not None:
        if (direction == "BULL" and spot > or_mid) or (direction == "BEAR" and spot < or_mid):
            score += 1

    aligned = bars_above if direction == "BULL" else bars_below
    print(f"  [ACCEPT] dir={direction} score={score} net7={net7:+.1f}pts slope={vwap_slope:+.3f} mom3={mom3:+.1f} aligned={aligned}/7")

    exits = build_exit_params(session, regime, atr, direction, spot, vwap)
    return {
        "signal_type": "VWAP_ACCEPTANCE",
        "trigger":     f"VWAP acceptance {direction} | {aligned}/7 bars aligned | net {net7:+.1f}pts",
        "bias":        direction,
        "score":       score,
        "quality":     "MEDIUM",
        "regime":      regime,
        "session":     session,
        "spot":        round(spot, 2),
        "vwap":        vwap,
        "momentum":    mom3,
        "atr":         atr,
        "vix":         vix,
        "level":       None,
        "level_name":  "",
        "time_stop":   "3:45 PM ET",
        "all_candidates": [],
        **exits,
    }


# ─────────────────────────────────────────
# ENGINE 6 — EARLY TREND CONTINUATION (LONG-ONLY)
# ─────────────────────────────────────────
def evaluate_early_trend_continuation_signal(bars, key_levels, vwap, vwap_history, vix, session, now_et,
                                             or_high=None, or_set=False):
    """
    Earlier long-only continuation signal for strong trend/grind days.
    Allows an earlier CALL signal when SPX is walking higher above VWAP,
    breaking structure, and not extended.
    """
    if not EARLY_TREND_ENABLED or not vwap:
        return None

    if get_regime(vix) == "HIGH_VOL":
        return None

    if now_et.time() < datetime.time(9, 50):
        return None

    rth_bars = [b for b in bars if b.get("t") and
                datetime.datetime.fromtimestamp(b["t"] / 1000, ET).time() >= datetime.time(9, 30)]
    if len(rth_bars) < EARLY_TREND_MIN_RTH_BARS:
        return None

    spot = bars[-1]["c"]
    atr = calc_atr(bars)
    regime = get_regime(vix)
    mom3 = calc_momentum(bars, 3)

    if spot <= vwap:
        return None

    vwap_dist = spot - vwap
    if vwap_dist > EARLY_TREND_MAX_DISTANCE_FROM_VWAP:
        print(f"  [EARLY_TREND] blocked: too extended {vwap_dist:.1f}pts above VWAP")
        return None

    prior_day_high = key_levels.get("Prev Day High") if key_levels else None
    above_pdh = prior_day_high is not None and spot > prior_day_high
    above_orh = or_set and or_high is not None and spot > or_high
    if not (above_pdh or above_orh):
        print("  [EARLY_TREND] blocked: not above PDH or OR high")
        return None

    vwap_slope = calc_vwap_slope(vwap_history, n=5)
    if vwap_slope < EARLY_TREND_VWAP_SLOPE_MIN:
        print(f"  [EARLY_TREND] blocked: VWAP slope {vwap_slope:+.3f} too weak")
        return None

    recent = rth_bars[-EARLY_TREND_PULLBACK_LOOKBACK:]
    if len(vwap_history) >= EARLY_TREND_PULLBACK_LOOKBACK:
        hist = vwap_history[-EARLY_TREND_PULLBACK_LOOKBACK:]
        closes_above_vwap = sum(1 for i, b in enumerate(recent) if b["c"] >= hist[i])
    else:
        closes_above_vwap = sum(1 for b in recent if b["c"] >= vwap)

    if closes_above_vwap < EARLY_TREND_MIN_CLOSES_ABOVE_VWAP:
        print(f"  [EARLY_TREND] blocked: only {closes_above_vwap}/8 closes above VWAP")
        return None

    for b in rth_bars[-3:]:
        if b["h"] > vwap and b["c"] < vwap:
            print("  [EARLY_TREND] blocked: recent VWAP rejection")
            return None

    if mom3 < EARLY_TREND_MIN_MOMENTUM:
        print(f"  [EARLY_TREND] blocked: mom3 {mom3:+.1f} < {EARLY_TREND_MIN_MOMENTUM}")
        return None

    latest = bars[-1]
    bar_range = latest["h"] - latest["l"]
    if bar_range > 0.5:
        upper_wick = latest["h"] - latest["c"]
        if upper_wick / bar_range > 0.45:
            print(f"  [EARLY_TREND] blocked: upper wick {upper_wick/bar_range:.0%}")
            return None

    score = 7
    if above_pdh:
        score += 1
    if above_orh:
        score += 1
    if vwap_dist <= atr * 0.75:
        score += 1
    if mom3 >= 4.0:
        score += 1

    structure = []
    if above_pdh:
        structure.append("above PDH")
    if above_orh:
        structure.append("above OR high")

    print(
        f"  [EARLY_TREND] LONG score={score} spot={spot:.1f} vwap={vwap:.1f} "
        f"dist={vwap_dist:.1f} slope={vwap_slope:+.3f} mom3={mom3:+.1f} "
        f"holds={closes_above_vwap}/8 {'/'.join(structure)}"
    )

    quality = "STRONG" if score >= 9 else "HIGH"
    exits = build_exit_params(session, regime, atr, "BULL", spot, vwap)

    return {
        "signal_type": "EARLY_TREND_CONTINUATION",
        "trigger":     f"Early trend continuation BULL | {', '.join(structure)} | VWAP rising | mom3 {mom3:+.1f}pts",
        "bias":        "BULL",
        "score":       score,
        "quality":     quality,
        "regime":      regime,
        "session":     session,
        "spot":        round(spot, 2),
        "vwap":        vwap,
        "momentum":    mom3,
        "atr":         atr,
        "vix":         vix,
        "level":       prior_day_high if above_pdh else or_high,
        "level_name":  "Prev Day High" if above_pdh else "OR High",
        "time_stop":   "3:45 PM ET",
        "all_candidates": [],
        **exits,
    }

# ─────────────────────────────────────────
# TWO-SPEED SYSTEM — CONTEXT → SETUP → TRIGGER
# ─────────────────────────────────────────

# Setup alert daily cap
MAX_SETUPS_PER_DAY = 5

def detect_context(bars, vwap, vwap_history, key_levels):
    """
    Derives current market context from existing data.
    Returns dict: bias, regime, location, near_level.
    """
    if not bars or not vwap:
        return {"bias": "NEUTRAL", "regime": "RANGE", "location": "UNKNOWN", "near_level": None}

    spot       = bars[-1]["c"]
    atr        = calc_atr(bars)
    vwap_slope = calc_vwap_slope(vwap_history, n=5)

    # Bias: price vs VWAP + slope
    if spot > vwap and vwap_slope >= -0.05:
        bias = "BULL"
    elif spot < vwap and vwap_slope <= 0.05:
        bias = "BEAR"
    else:
        bias = "NEUTRAL"

    # Regime: TREND / COMPRESSION / RANGE
    rth_bars = [b for b in bars if b.get("t") and
                datetime.datetime.fromtimestamp(b["t"] / 1000, ET).time() >= datetime.time(9, 30)]
    regime = "RANGE"
    if len(rth_bars) >= 20:
        recent20   = rth_bars[-20:]
        above_vwap = sum(1 for b in recent20 if b["c"] > vwap)
        net20      = recent20[-1]["c"] - recent20[0]["c"]
        if above_vwap >= 14 and net20 >= 10:
            regime = "TREND"
        elif above_vwap <= 6 and net20 <= -10:
            regime = "TREND"
        elif len(bars) >= 20:
            avg_recent = sum(b["h"] - b["l"] for b in bars[-5:]) / 5
            avg_prior  = sum(b["h"] - b["l"] for b in bars[-20:-5]) / 15
            if avg_prior > 0 and avg_recent < avg_prior * 0.60:
                regime = "COMPRESSION"

    # Location: near VWAP or key level
    location   = "ABOVE_VWAP" if spot > vwap else "BELOW_VWAP"
    near_level = None
    if abs(spot - vwap) <= 5:
        location   = "AT_LEVEL"
        near_level = "VWAP"
    elif key_levels:
        for name, level in key_levels.items():
            if level and name != "VWAP" and abs(spot - level) <= 5:
                location   = "AT_LEVEL"
                near_level = name
                break

    return {"bias": bias, "regime": regime, "location": location, "near_level": near_level}


# Priority zone widths
PRIORITY_ZONE_WIDTH  = {"HIGH": 6, "MEDIUM": 4, "LOW": 3}
PRIORITY_WEIGHT      = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
CLUSTER_WINDOW       = 15.0   # pts — group levels within this range into a cluster
PROXIMITY_FILTER     = 25.0   # pts — ignore levels outside ±this from spot
PRE_BREAK_INNER      = 5.0    # pts — inner pressure zone boundary
PRE_BREAK_OUTER      = 10.0   # pts — outer pressure zone boundary
AIR_POCKET_MIN       = 20.0   # pts — gap to next HIGH zone triggering expansion bonus


def parse_levels(raw_levels):
    """
    Parse MANUAL_LEVELS which may be:
      - New format: {name: (price, priority)}
      - Legacy format: {name: price}

    Returns {name: {"price": float, "priority": str}}
    """
    parsed = {}
    for name, val in raw_levels.items():
        if isinstance(val, tuple):
            price, priority = val[0], val[1]
        else:
            price    = float(val)
            # Infer priority from name
            if any(s in name for s in ["Daily 1SD", "Weekly 1SD", "ATH VWAP", "WTD VWAP"]):
                priority = "HIGH"
            elif any(s in name for s in ["Gamma", "Yearly", "Monthly"]):
                priority = "LOW"
            else:
                priority = "MEDIUM"
        parsed[name] = {"price": float(price), "priority": priority}
    return parsed


def filter_vwap_levels(parsed_levels, spot):
    """
    Keep only the 2 closest VWAP-type levels above spot and 2 below.
    Non-VWAP levels are kept unchanged.
    Reduces VWAP noise in level dict.
    """
    vwap_names  = [n for n in parsed_levels if "VWAP" in n and n != "VWAP"]
    other_names = [n for n in parsed_levels if "VWAP" not in n or n == "VWAP"]

    above = sorted([n for n in vwap_names if parsed_levels[n]["price"] > spot],
                   key=lambda n: parsed_levels[n]["price"])[:2]
    below = sorted([n for n in vwap_names if parsed_levels[n]["price"] <= spot],
                   key=lambda n: parsed_levels[n]["price"], reverse=True)[:2]

    keep = set(other_names) | set(above) | set(below)
    return {n: parsed_levels[n] for n in keep if n in parsed_levels}


def cluster_levels(key_levels, spot, proximity=CLUSTER_WINDOW, parsed=None):
    """
    Group levels within CLUSTER_WINDOW pts into clusters.
    Only considers levels within PROXIMITY_FILTER of spot.

    Each cluster:
      - cluster_price: weighted average (HIGH=3, MEDIUM=2, LOW=1)
      - cluster_strength: sum of weights (higher = stronger)
      - priority: highest priority among members
      - role: SUPPORT if spot > cluster_price, else RESISTANCE
      - zone_width: based on top priority
      - low/high: actual price boundaries of members
    """
    if not key_levels:
        return []

    # Build flat list, apply proximity filter
    items = []
    for name, val in key_levels.items():
        if name == "VWAP":
            continue
        if parsed and name in parsed:
            price    = parsed[name]["price"]
            priority = parsed[name]["priority"]
        elif isinstance(val, tuple):
            price, priority = float(val[0]), val[1]
        elif val is not None:
            price    = float(val)
            priority = "MEDIUM"
        else:
            continue
        # Proximity filter — ignore far levels
        if abs(price - spot) > PROXIMITY_FILTER:
            continue
        items.append((name, price, priority))

    items.sort(key=lambda x: x[1])

    clusters = []
    i = 0
    while i < len(items):
        name, price, priority = items[i]
        group = [(name, price, priority)]
        j = i + 1
        while j < len(items) and items[j][1] - group[0][1] <= proximity:
            group.append(items[j])
            j += 1

        prices     = [c[1] for c in group]
        priorities = [c[2] for c in group]
        weights    = [PRIORITY_WEIGHT.get(c[2], 1) for c in group]
        top_priority = max(priorities, key=lambda p: PRIORITY_WEIGHT.get(p, 0))

        # Weighted average price
        total_weight   = sum(weights)
        cluster_price  = round(sum(p * w for (_, p, _), w in zip(group, weights)) / total_weight, 2)
        cluster_strength = total_weight  # higher = more levels/higher priority

        low  = min(prices)
        high = max(prices)
        zone_width = PRIORITY_ZONE_WIDTH.get(top_priority, 4)
        label = f"Zone {low:.0f}–{high:.0f}" if len(group) > 1 else group[0][0]
        role  = "SUPPORT" if spot > cluster_price else "RESISTANCE"

        clusters.append({
            "name":             label,
            "low":              low,
            "high":             high,
            "mid":              cluster_price,   # weighted avg, not simple midpoint
            "members":          [c[0] for c in group],
            "priority":         top_priority,
            "cluster_strength": cluster_strength,
            "zone_width":       zone_width,
            "role":             role,
        })
        i = j
    return clusters


def assign_zone_roles(zones, spot):
    """Refresh role (SUPPORT/RESISTANCE) on each zone."""
    for zone in zones:
        zone["role"] = "SUPPORT" if spot > zone["mid"] else "RESISTANCE"
    return zones


def find_nearest_zone(spot, zones, min_priority="MEDIUM"):
    """
    Return nearest cluster within PRE_BREAK_OUTER pts of spot.
    Only considers clusters at or above min_priority.
    """
    min_rank = PRIORITY_WEIGHT.get(min_priority, 2)
    nearest, nearest_dist = None, float("inf")
    for zone in zones:
        if PRIORITY_WEIGHT.get(zone["priority"], 0) < min_rank:
            continue
        dist = min(abs(spot - zone["low"]), abs(spot - zone["high"]), abs(spot - zone["mid"]))
        if dist <= PRE_BREAK_OUTER and dist < nearest_dist:
            nearest_dist = dist
            nearest = zone
    return nearest


def find_next_zone_in_direction(spot, zones, direction, min_priority="HIGH"):
    """
    Return the next HIGH zone in the given direction (BULL=above, BEAR=below).
    Used for air pocket detection and alert context.
    """
    min_rank = PRIORITY_WEIGHT.get(min_priority, 3)
    candidates = []
    for zone in zones:
        if PRIORITY_WEIGHT.get(zone["priority"], 0) < min_rank:
            continue
        if direction == "BULL" and zone["mid"] > spot:
            candidates.append(zone)
        elif direction == "BEAR" and zone["mid"] < spot:
            candidates.append(zone)
    if not candidates:
        return None
    return min(candidates, key=lambda z: abs(z["mid"] - spot))


def detect_pre_break_pressure(spot, bars, atr, zones, vwap, now_et):
    """
    Detect pre-breakout pressure conditions near a cluster.
    Returns (pressure_zone, score_boost, description) or (None, 0, "").

    Conditions (all required):
      1. Nearest HIGH/MEDIUM cluster within PRE_BREAK_OUTER (10pts)
      2. Directional alignment: spot < zone.mid → BULL pressure (mom3 > 0 required)
                                spot > zone.mid → BEAR pressure (mom3 < 0 required)
      3. Compression confirmed (calc_compression_score > 0) — energy must be coiling
      4. Time >= 9:45 ET

    Scoring:
      Base: +1 outer zone (<=10pts), +2 inner zone (<=5pts)
      +1 cluster_strength >= 6 (multiple confluent levels)
      +2 air pocket >= AIR_POCKET_MIN pts to next HIGH zone
    """
    if now_et.time() < datetime.time(9, 45) or len(bars) < 6:
        return None, 0, ""

    # Fix 1: compression as boost, not gate
    atr_val = atr or calc_atr(bars)
    compression_score, _ = calc_compression_score(bars, atr_val, {}, now_et)

    mom3    = calc_momentum(bars, 3)
    nearest = find_nearest_zone(spot, zones, min_priority="MEDIUM")
    if not nearest:
        return None, 0, ""

    dist_to_low  = spot - nearest["low"]
    dist_to_high = nearest["high"] - spot

    if spot < nearest["mid"]:
        expected_direction = "BULL"
        approaching = 0 < dist_to_high <= PRE_BREAK_OUTER
    else:
        expected_direction = "BEAR"
        approaching = 0 < dist_to_low <= PRE_BREAK_OUTER

    if not approaching:
        return None, 0, ""

    if expected_direction == "BULL" and mom3 <= 0:
        return None, 0, ""
    if expected_direction == "BEAR" and mom3 >= 0:
        return None, 0, ""

    dist = dist_to_high if expected_direction == "BULL" else dist_to_low

    boost = 2 if dist <= PRE_BREAK_INNER else 1

    # Compression bonus (not gate)
    if compression_score > 0:
        boost += 1

    # Cluster strength bonus
    if nearest.get("cluster_strength", 0) >= 6:
        boost += 1

    # Fix 3: air pocket — no next zone = open air
    context_note = ""
    next_zone = find_next_zone_in_direction(spot, zones, expected_direction, min_priority="HIGH")
    if not next_zone:
        boost += 2
        context_note = " | Open air (no resistance/support beyond)"
    elif abs(next_zone["mid"] - spot) >= AIR_POCKET_MIN:
        gap = abs(next_zone["mid"] - spot)
        boost += 2
        context_note = f" | Air pocket → {gap:.0f}pts to {next_zone['name']}"

    desc = (f"{'Pressing' if dist <= PRE_BREAK_INNER else 'Approaching'} "
            f"{nearest['name']} from {'below' if expected_direction == 'BULL' else 'above'} "
            f"({dist:.1f}pts) | mom3 {mom3:+.1f}{context_note}")

    return nearest, boost, desc


def detect_air_pocket(spot, zones, direction):
    """
    If price just cleared a HIGH zone and next HIGH zone is >AIR_POCKET_MIN away,
    return (True, next_zone, gap_size). Otherwise (False, None, 0).
    """
    next_zone = find_next_zone_in_direction(spot, zones, direction, min_priority="HIGH")
    if not next_zone:
        return True, None, 999  # no resistance above/below = air pocket
    gap = abs(next_zone["mid"] - spot)
    if gap >= AIR_POCKET_MIN:
        return True, next_zone, round(gap, 1)
    return False, next_zone, round(gap, 1)


def format_zone_context(spot, zones, vwap):
    """
    Build the 'Key Zone' context string for Telegram alerts.
    Shows nearest cluster + above/below targets.
    """
    nearest = find_nearest_zone(spot, zones, min_priority="MEDIUM")
    above   = find_next_zone_in_direction(spot, zones, "BULL", min_priority="MEDIUM")
    below   = find_next_zone_in_direction(spot, zones, "BEAR", min_priority="MEDIUM")

    lines = []
    if nearest:
        lines.append(f"*Key Zone:* {nearest['name']} [{nearest['priority']}] | {nearest['role']}")
    if above:
        air, _, gap = detect_air_pocket(spot, zones, "BULL")
        suffix = " (open air)" if air and gap >= AIR_POCKET_MIN else ""
        lines.append(f"*Above →* {above['name']} ({above['mid']:.0f}){suffix}")
    if below:
        lines.append(f"*Below →* {below['name']} ({below['mid']:.0f})")
    if vwap:
        vwap_role = "above" if spot > vwap else "below"
        lines.append(f"*VWAP:* {vwap:,.2f} (price {vwap_role})")
    return "\n".join(lines)


def detect_setup(context, bars, vwap, vwap_history, key_levels, or_high=None, or_low=None):
    """
    Detects early structural setups. Returns setup dict or None.

    Types:
      PRE_BREAK_SETUP          — pressure building near cluster (earliest signal)
      COMPRESSION_SETUP        — range contracting near HIGH/MEDIUM cluster
      VWAP_RECLAIM_SETUP       — price crossing and holding above/below VWAP
      TREND_CONTINUATION_SETUP — trend walking above VWAP with shallow pullbacks

    Level handling:
      - Parses (price, priority) format
      - Filters VWAPs to 2 above + 2 below
      - Clusters within 15pts with weighted average price
      - Proximity filter: only clusters within ±25pts of spot
      - PRE_BREAK fires before full breakout confirmation
    """
    if not bars or not vwap:
        return None

    spot   = bars[-1]["c"]
    atr    = calc_atr(bars)
    bias   = context["bias"]
    regime = context["regime"]
    now_et = datetime.datetime.now(ET)

    # Parse, filter, cluster
    parsed   = parse_levels(key_levels) if key_levels else {}
    filtered = filter_vwap_levels(parsed, spot)
    flat     = {n: v["price"] for n, v in filtered.items()}
    zones    = cluster_levels(flat, spot, proximity=CLUSTER_WINDOW, parsed=filtered)
    zones    = assign_zone_roles(zones, spot)
    ctx_str  = format_zone_context(spot, zones, vwap)

    # ── PRE_BREAK_SETUP — earliest signal, fires before compression confirmed ─
    if now_et.time() >= datetime.time(9, 45) and len(bars) >= 6:
        pressure_zone, boost, pressure_desc = detect_pre_break_pressure(
            spot, bars, atr, zones, vwap, now_et
        )
        if pressure_zone and boost > 0:
            # Direction from zone position: spot below mid = BULL, spot above mid = BEAR
            direction = "BULL" if spot < pressure_zone["mid"] else "BEAR"
            air, next_z, gap = detect_air_pocket(spot, zones, direction)
            air_str = f" → {next_z['name']} ({gap:.0f}pts, open air)" if air and next_z else ""
            return {
                "type":        "PRE_BREAK_SETUP",
                "bias":        direction,
                "level":       pressure_zone["name"],
                "level_price": pressure_zone["mid"],
                "level_low":   pressure_zone["low"],
                "level_high":  pressure_zone["high"],
                "zone_width":  pressure_zone["zone_width"],
                "role":        pressure_zone["role"],
                "priority":    pressure_zone["priority"],
                "pre_break_boost": boost,
                "message":     (f"Pressure building near {pressure_zone['name']} "
                               f"[{pressure_zone['priority']}] — {pressure_desc}{air_str}"),
                "zone_context": ctx_str,
                "spot":        round(spot, 2),
                "vwap":        vwap,
                "atr":         atr,
            }

    # ── COMPRESSION_SETUP ────────────────────────────────────────────────────
    if regime == "COMPRESSION":
        near_zone = find_nearest_zone(spot, zones, min_priority="MEDIUM")
        if near_zone:
            compression_confirmed = False
            if len(bars) >= 20:
                avg_recent = sum(b["h"] - b["l"] for b in bars[-5:]) / 5
                avg_prior  = sum(b["h"] - b["l"] for b in bars[-20:-5]) / 15
                compression_confirmed = avg_prior > 0 and avg_recent < avg_prior * COMPRESSION_RANGE_RATIO
            near_boundary = (abs(spot - near_zone["low"]) <= near_zone["zone_width"] or
                             abs(spot - near_zone["high"]) <= near_zone["zone_width"])
            if compression_confirmed and near_boundary:
                role_bias = "BULL" if near_zone["role"] == "SUPPORT" else "BEAR"
                direction = role_bias if bias == "NEUTRAL" else bias
                air, next_z, gap = detect_air_pocket(spot, zones, direction)
                air_str = f" → {next_z['name']} open air" if air and next_z else ""
                return {
                    "type":        "COMPRESSION_SETUP",
                    "bias":        direction,
                    "level":       near_zone["name"],
                    "level_price": near_zone["mid"],
                    "level_low":   near_zone["low"],
                    "level_high":  near_zone["high"],
                    "zone_width":  near_zone["zone_width"],
                    "role":        near_zone["role"],
                    "priority":    near_zone["priority"],
                    "message":     (f"Compression near {near_zone['name']} "
                                   f"[{near_zone['priority']}] — {near_zone['role']} zone{air_str}"),
                    "zone_context": ctx_str,
                    "spot":        round(spot, 2),
                    "vwap":        vwap,
                    "atr":         atr,
                }

    # ── VWAP_RECLAIM_SETUP ───────────────────────────────────────────────────
    if len(bars) >= 3:
        rth_bars = [b for b in bars if b.get("t") and
                    datetime.datetime.fromtimestamp(b["t"] / 1000, ET).time() >= datetime.time(9, 30)]
        if len(rth_bars) >= 3:
            last3 = rth_bars[-3:]
            was_below   = last3[0]["c"] < vwap
            now_above   = sum(1 for b in last3[1:] if b["c"] > vwap) >= 2
            net_reclaim = spot - last3[0]["c"]
            if was_below and now_above and spot > vwap and net_reclaim >= 2.0:
                air, next_z, gap = detect_air_pocket(spot, zones, "BULL")
                air_str = f" → {next_z['name']} ({gap:.0f}pts)" if next_z else ""
                return {
                    "type":        "VWAP_RECLAIM_SETUP",
                    "bias":        "BULL",
                    "level":       "VWAP",
                    "level_price": vwap,
                    "level_low":   vwap,
                    "level_high":  vwap,
                    "zone_width":  4,
                    "role":        "SUPPORT",
                    "priority":    "HIGH",
                    "message":     f"VWAP reclaim above {vwap:,.2f} — potential long{air_str}",
                    "zone_context": ctx_str,
                    "spot":        round(spot, 2),
                    "vwap":        vwap,
                    "atr":         atr,
                }
            was_above     = last3[0]["c"] > vwap
            now_below     = sum(1 for b in last3[1:] if b["c"] < vwap) >= 2
            net_rejection = last3[0]["c"] - spot
            if was_above and now_below and spot < vwap and net_rejection >= 2.0:
                air, next_z, gap = detect_air_pocket(spot, zones, "BEAR")
                air_str = f" → {next_z['name']} ({gap:.0f}pts)" if next_z else ""
                return {
                    "type":        "VWAP_RECLAIM_SETUP",
                    "bias":        "BEAR",
                    "level":       "VWAP",
                    "level_price": vwap,
                    "level_low":   vwap,
                    "level_high":  vwap,
                    "zone_width":  4,
                    "role":        "RESISTANCE",
                    "priority":    "HIGH",
                    "message":     f"VWAP rejection below {vwap:,.2f} — potential short{air_str}",
                    "zone_context": ctx_str,
                    "spot":        round(spot, 2),
                    "vwap":        vwap,
                    "atr":         atr,
                }

    # ── TREND_CONTINUATION_SETUP ──────────────────────────────────────────────
    if regime == "TREND" and bias != "NEUTRAL":
        vwap_slope    = calc_vwap_slope(vwap_history, n=5)
        slope_aligned = (bias == "BULL" and vwap_slope >= 0.05) or (bias == "BEAR" and vwap_slope <= -0.05)
        vwap_dist     = abs(spot - vwap)
        not_extended  = vwap_dist <= calc_atr(bars) * 1.25
        rth_tc = [b for b in bars if b.get("t") and
                  datetime.datetime.fromtimestamp(b["t"] / 1000, ET).time() >= datetime.time(9, 30)]
        closes_aligned = 0
        net15 = 0.0
        if len(rth_tc) >= 15:
            last15 = rth_tc[-15:]
            if len(vwap_history) >= 15:
                hist15 = vwap_history[-15:]
                closes_aligned = sum(
                    1 for i, b in enumerate(last15)
                    if (bias == "BULL" and b["c"] > hist15[i]) or
                       (bias == "BEAR" and b["c"] < hist15[i])
                )
            else:
                closes_aligned = sum(
                    1 for b in last15
                    if (bias == "BULL" and b["c"] > vwap) or (bias == "BEAR" and b["c"] < vwap)
                )
            net15 = last15[-1]["c"] - last15[0]["c"]
        sufficient_closes = closes_aligned >= 12
        sufficient_move   = (bias == "BULL" and net15 >= 8.0) or (bias == "BEAR" and net15 <= -8.0)
        if slope_aligned and not_extended and sufficient_closes and sufficient_move:
            direction_word = "higher" if bias == "BULL" else "lower"
            air, next_z, gap = detect_air_pocket(spot, zones, bias)
            air_str = f" → {next_z['name']} ({gap:.0f}pts)" if next_z else ""
            return {
                "type":        "TREND_CONTINUATION_SETUP",
                "bias":        bias,
                "level":       "VWAP",
                "level_price": vwap,
                "level_low":   vwap,
                "level_high":  vwap,
                "zone_width":  4,
                "role":        "SUPPORT" if bias == "BULL" else "RESISTANCE",
                "priority":    "HIGH",
                "message":     (f"Trend {direction_word} | {closes_aligned}/15 aligned | "
                               f"net {net15:+.1f}pts{air_str}"),
                "zone_context": ctx_str,
                "spot":        round(spot, 2),
                "vwap":        vwap,
                "atr":         atr,
            }

    return None


def format_setup_message(setup, or_high=None, or_low=None):
    """Format SETUP awareness alert for Telegram."""
    emoji = "\U0001f7e1"  # yellow circle
    bias_str  = setup["bias"]
    direction = "LONG" if bias_str == "BULL" else "SHORT" if bias_str == "BEAR" else "WATCH"
    t = datetime.datetime.now(ET).strftime("%I:%M %p ET")
    priority  = setup.get("priority", "")
    role      = setup.get("role", "")
    zone_ctx  = setup.get("zone_context", "")

    msg = (
        f"{emoji} *SETUP: {setup['type']}*\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"*Direction:* {direction} | *Zone:* {setup.get('level', '')} [{priority}]\n"
        f"*Role:*      {role}\n"
        f"*Time:*      {t}\n"
        f"*SPX:*       {setup['spot']:,.2f} | VWAP: {setup['vwap']:,.2f}\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"{setup['message']}\n"
    )
    if zone_ctx:
        msg += f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n{zone_ctx}\n"
    msg += f"_No trade yet — waiting for trigger confirmation_"
    return msg


def build_synthetic_bar(ticks):
    """
    Build a synthetic OHLC bar from a list of (timestamp, price) tuples.
    Includes tick_count so trigger engine knows if wick data is reliable.
    Returns bar dict compatible with existing engines.
    """
    if not ticks:
        return None
    prices = [p for _, p in ticks]
    return {
        "o":          prices[0],
        "h":          max(prices),
        "l":          min(prices),
        "c":          prices[-1],
        "v":          1,
        "t":          int(ticks[-1][0] * 1000),
        "tick_count": len(ticks),
    }


def detect_fast_trigger(synth_bars, setup, atr, vwap, setup_start_time, now_et, synthetic_ticks=None):
    """
    Fast trigger engine. Two-tier:

    PRIMARY — raw tick acceleration (fires without waiting for 3 synthetic bars):
      BULL: price_now - price_30s_ago >= 2.0 AND price_now - price_60s_ago >= 3.0
      BEAR: inverse
      + anti-chase limits still apply

    SECONDARY — synthetic bar quality check (optional wick filter):
      Only applies wick rejection if synth bar has tick_count >= 3.

    10-minute time limit, 1.5 ATR anti-chase, anti-extension enforced.
    """
    if not setup:
        return None

    bias         = setup["bias"]
    setup_origin = setup.get("spot", 0)
    atr_val      = atr or 5.0

    # Time limit: trigger must occur within 10 minutes of setup
    if setup_start_time and (now_et - setup_start_time).total_seconds() > 600:
        return None

    # ── PRIMARY: raw tick-window trigger ─────────────────────────────────────
    if synthetic_ticks and len(synthetic_ticks) >= 2:
        now_ts    = now_et.timestamp()
        price_now = synthetic_ticks[-1][1]

        # Anti-chase pre-check
        if vwap and abs(price_now - vwap) > 15.0:
            pass  # fall through to synthetic bar check
        elif atr_val and abs(price_now - setup_origin) > atr_val * 1.5:
            pass  # fall through
        else:
            # Find price 30s and 60s ago
            price_30s = next((p for ts, p in reversed(synthetic_ticks) if now_ts - ts >= 30), None)
            price_60s = next((p for ts, p in reversed(synthetic_ticks) if now_ts - ts >= 60), None)

            bull_trigger = (
                price_30s is not None and price_now - price_30s >= 2.0 and
                (price_60s is None or price_now - price_60s >= 3.0)
            )
            bear_trigger = (
                price_30s is not None and price_30s - price_now >= 2.0 and
                (price_60s is None or price_60s - price_now >= 3.0)
            )

            # Zone boundary check: require clean break of zone boundary, not just midpoint
            zone_high = setup.get("level_high", setup.get("level_price", price_now))
            zone_low  = setup.get("level_low",  setup.get("level_price", price_now))
            if bias == "BULL":
                bull_trigger = bull_trigger and price_now > zone_high
            if bias == "BEAR":
                bear_trigger = bear_trigger and price_now < zone_low

            triggered = (bias == "BULL" and bull_trigger) or (bias == "BEAR" and bear_trigger)
            if triggered:
                net_raw = price_now - (price_30s or price_now)
                return {
                    "confirmed":    True,
                    "bias":         bias,
                    "spot":         round(price_now, 2),
                    "net3":         round(net_raw, 2),
                    "accelerating": True,
                    "setup_type":   setup["type"],
                    "trigger_mode": "RAW_TICK",
                }

    # ── SECONDARY: synthetic bar trigger (quality check) ─────────────────────
    if not synth_bars or len(synth_bars) < 3:
        return None

    spot = synth_bars[-1]["c"]
    net3 = synth_bars[-1]["c"] - synth_bars[-3]["c"]

    if bias == "BULL" and net3 < 2.0:
        return None
    if bias == "BEAR" and net3 > -2.0:
        return None

    if vwap and abs(spot - vwap) > 15.0:
        return None
    if vwap and atr_val and abs(spot - vwap) > atr_val * 1.5:
        return None
    if atr_val and abs(spot - setup_origin) > atr_val * 1.5:
        return None

    # Wick check only if bar has enough ticks
    bar = synth_bars[-1]
    tick_count = bar.get("tick_count", 1)
    if tick_count >= 3:
        br = bar["h"] - bar["l"]
        if br > 0.5:
            if bias == "BULL" and (bar["h"] - bar["c"]) / br > 0.45:
                return None
            if bias == "BEAR" and (bar["c"] - bar["l"]) / br > 0.45:
                return None

    net_last  = synth_bars[-1]["c"] - synth_bars[-2]["c"]
    net_prior = synth_bars[-2]["c"] - synth_bars[-3]["c"]
    accelerating = (bias == "BULL" and net_last > 0 and net_last >= net_prior) or \
                   (bias == "BEAR" and net_last < 0 and net_last <= net_prior)

    return {
        "confirmed":    True,
        "bias":         bias,
        "spot":         round(spot, 2),
        "net3":         round(net3, 2),
        "accelerating": accelerating,
        "setup_type":   setup["type"],
        "trigger_mode": "SYNTH_BAR",
    }


def format_trigger_message(trigger, setup, closed_bars, vwap, vix, session, atr):
    """Format TRIGGER tradeable alert with strike suggestion."""
    t     = datetime.datetime.now(ET).strftime("%I:%M %p ET")
    bias  = trigger["bias"]
    spot  = trigger["spot"]
    emoji = "\U0001f7e2" if bias == "BULL" else "\U0001f534"
    regime = get_regime(vix)

    exits = build_exit_params(session, regime, atr, bias, spot, vwap)
    vix_str  = f"{vix:.1f}" if vix else "N/A"
    vwap_str = f"{vwap:,.2f}" if vwap else "N/A"

    mode_str = "raw tick acceleration" if trigger.get("trigger_mode") == "RAW_TICK" else "over fast window"
    msg = (
        f"{emoji} *TRIGGER: {trigger['setup_type']}*\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"*{exits['option_type']} 0DTE* | Confirmed\n"
        f"*Time:*     {t}\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"*Spot:*     {spot:,.2f}\n"
        f"*Strike:*   {exits['strike']} {exits['option_type']} 0DTE\n"
        f"*VWAP:*     {vwap_str}\n"
        f"*Micro-mom:* {trigger['net3']:+.1f}pts via {mode_str}\n"
        f"*VIX:*      {vix_str}\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"*T1:* {exits['target_1']:,.2f}  |  *T2:* {exits['target_2']:,.2f}\n"
        f"*Stop:* -{exits['stop_pct']}% premium  |  SPX {exits['invalidate']:,.2f}\n"
        f"*No chase:* past {exits['no_chase']:,.2f}\n"
        f"*Exit by:* 3:45 PM ET"
    )
    # Add zone context from setup if available
    zone_ctx = setup.get("zone_context", "") if setup else ""
    if zone_ctx:
        msg += f"\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n{zone_ctx}"
    return msg

# ─────────────────────────────────────────
# COMBINED EVALUATOR
# ─────────────────────────────────────────
def evaluate_signal(bars, key_levels, vwap, vwap_history, vix, session,
                    last_signal_time, last_signal_price, last_signal_bias, or_set,
                    or_high=None, or_low=None):
    now_et = datetime.datetime.now(ET)
    if last_signal_time:
        if (now_et - last_signal_time).total_seconds() / 60 < COOLDOWN_MINUTES:
            return None

    early_trend_sig = evaluate_early_trend_continuation_signal(
        bars, key_levels, vwap, vwap_history, vix, session, now_et,
        or_high=or_high, or_set=or_set
    )
    mom_sig    = evaluate_momentum_signal(bars, vwap, vwap_history, vix, session,
                                          key_levels=key_levels, now_et=now_et)
    brk_sig    = evaluate_breakout_signal(bars, key_levels, vwap, vix, session, or_set)
    trap_sig   = evaluate_trap_signal(bars, key_levels, vwap, vix, session, or_set)
    grind_sig  = evaluate_trend_grind_signal(bars, vwap, vwap_history, vix, session, now_et)
    accept_sig = evaluate_vwap_acceptance_signal(bars, vwap, vwap_history, vix, session, now_et,
                                                  or_high=or_high, or_low=or_low)

    # Rank all engines — pick highest score
    candidates = [s for s in [early_trend_sig, mom_sig, brk_sig, trap_sig, grind_sig, accept_sig] if s is not None]
    if not candidates:
        return None

    def rank_key(s):
        base = s["score"]
        # Tiebreak: momentum > early trend > trap > grind > acceptance > breakout
        type_bonus = {
            "MOMENTUM": 0.5,
            "EARLY_TREND_CONTINUATION": 0.4,
            "TRAP": 0.3,
            "TREND_GRIND": 0.2,
            "VWAP_ACCEPTANCE": 0.1,
            "VWAP_ACCEPTANCE_EARLY": 0.05,
        }.get(s["signal_type"], 0)
        return base + type_bonus

    candidates.sort(key=rank_key, reverse=True)

    # Early regime shift priority — only override when no truly strong non-early signal exists
    early_sig = next((s for s in candidates if s["signal_type"] == "VWAP_ACCEPTANCE_EARLY"), None)
    non_early = [s for s in candidates if s["signal_type"] != "VWAP_ACCEPTANCE_EARLY"]
    top_non_early = non_early[0] if non_early else None
    if early_sig:
        if top_non_early:
            stronger_signal_exists = (
                top_non_early["score"] >= early_sig["score"] + 3
                and top_non_early["score"] > 8
            )
        else:
            stronger_signal_exists = False
        if not stronger_signal_exists:
            if last_signal_price and last_signal_bias == early_sig["bias"]:
                if abs(early_sig["spot"] - last_signal_price) < MIN_SIGNAL_DISTANCE:
                    print("  -> EARLY distance filter blocked")
                else:
                    print(
                        f"  -> EARLY signal prioritized over "
                        f"{top_non_early['signal_type'] if top_non_early else 'none'}="
                        f"{top_non_early['score'] if top_non_early else 'N/A'}"
                    )
                    return early_sig
            else:
                print(
                    f"  -> EARLY signal prioritized over "
                    f"{top_non_early['signal_type'] if top_non_early else 'none'}="
                    f"{top_non_early['score'] if top_non_early else 'N/A'}"
                )
                return early_sig
        else:
            print(
                f"  -> EARLY not prioritized: stronger "
                f"{top_non_early['signal_type']} score={top_non_early['score']} "
                f"vs early={early_sig['score']}"
            )
    best = candidates[0]

    if len(candidates) > 1:
        others = ", ".join(f"{s['signal_type']}={s['score']}" for s in candidates[1:])
        print(f"  -> {best['signal_type']} wins score={best['score']} (vs {others})")

    if last_signal_price and last_signal_bias == best["bias"]:
        if abs(best["spot"] - last_signal_price) < MIN_SIGNAL_DISTANCE:
            print(f"  -> Distance filter blocked")
            return None

    if session == "AFTERNOON" and "OR" in best.get("level_name", ""):
        print(f"  -> OR suppressed in AFTERNOON")
        return None

    return best

# ─────────────────────────────────────────
# SIGNAL LOGGING
# ─────────────────────────────────────────
SIGNAL_LOG_FILE   = "signal_log.csv"
SIGNAL_LOG_FIELDS = [
    "timestamp", "signal_type", "trigger", "score", "quality", "regime", "session",
    "bias", "spot", "vwap", "momentum", "atr", "level", "target_1", "target_2", "invalidate"
]

def log_signal(sig):
    try:
        write_header = not os.path.exists(SIGNAL_LOG_FILE)
        with open(SIGNAL_LOG_FILE, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=SIGNAL_LOG_FIELDS)
            if write_header:
                writer.writeheader()
            writer.writerow({
                "timestamp":   datetime.datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET"),
                "signal_type": sig.get("signal_type", ""),
                "trigger":     sig.get("trigger", ""),
                "score":       sig.get("score", ""),
                "quality":     sig.get("quality", ""),
                "regime":      sig.get("regime", ""),
                "session":     sig.get("session", ""),
                "bias":        sig.get("bias", ""),
                "spot":        sig.get("spot", ""),
                "vwap":        sig.get("vwap", ""),
                "momentum":    sig.get("momentum", ""),
                "atr":         sig.get("atr", ""),
                "level":       sig.get("level", ""),
                "target_1":    sig.get("target_1", ""),
                "target_2":    sig.get("target_2", ""),
                "invalidate":  sig.get("invalidate", ""),
            })
    except Exception as e:
        print(f"[WARN] Signal log failed: {e}")

# ─────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────
_telegram_queue  = queue.Queue(maxsize=100)

def _telegram_worker():
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    while True:
        message = _telegram_queue.get()
        if message is None:
            break
        try:
            r = requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"
            }, timeout=10)
            if r.status_code == 200:
                print(f"[TELEGRAM SENT]")
            else:
                print(f"[TELEGRAM ERROR] {r.status_code}: {r.text[:100]}")
        except Exception as e:
            print(f"[TELEGRAM ERROR] {e}")
        finally:
            _telegram_queue.task_done()

_telegram_thread = threading.Thread(target=_telegram_worker, daemon=True)
_telegram_thread.start()

def send_telegram(message):
    try:
        _telegram_queue.put_nowait(message)
    except queue.Full:
        print(f"[TELEGRAM] Queue full - dropped")

def format_signal_message(sig, alert_count):
    t     = datetime.datetime.now(ET).strftime("%I:%M %p ET")
    emoji = "\U0001f7e2" if sig["bias"] == "BULL" else "\U0001f534"
    type_label = sig.get("signal_type", "SIGNAL")
    quality_badges = {"STRONG": "\u2b50\u2b50 STRONG", "HIGH": "\u2b50 HIGH", "MEDIUM": "MEDIUM"}
    badge = quality_badges.get(sig["quality"], sig["quality"])
    regime_labels = {"HIGH_VOL": "\U0001f525 High Vol", "ELEVATED": "\u26a1 Elevated", "NORMAL": "\u2705 Normal"}
    regime_str = regime_labels.get(sig["regime"], sig["regime"])
    vix_str  = f"{sig['vix']:.1f}" if sig.get("vix") else "N/A"
    vwap_str = f"{sig['vwap']:,.2f}" if sig.get("vwap") else "N/A"
    msg = (
        f"*SPX 0DTE {type_label} {emoji} {sig['option_type']}*\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"*Quality:*  {badge} (score: {sig['score']})\n"
        f"*Regime:*   {regime_str} | {sig['session']}\n"
        f"*Trigger:*  {sig['trigger']}\n"
        f"*Time:*     {t}\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"*Spot:*     {sig['spot']:,.2f}\n"
        f"*Strike:*   {sig['strike']} {sig['option_type']} 0DTE\n"
        f"*VWAP:*     {vwap_str}\n"
        f"*Momentum:* {sig['momentum']:+.1f} pts  |  *ATR:* {sig['atr']:.1f}\n"
        f"*VIX:*      {vix_str}\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"*T1:* {sig['target_1']:,.2f}  |  *T2:* {sig['target_2']:,.2f}\n"
        f"*Stop:* -{sig['stop_pct']}% premium  |  SPX {sig['invalidate']:,.2f}\n"
        f"*No chase:* past {sig['no_chase']:,.2f}\n"
        f"*Exit by:* {sig['time_stop']}\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"Alert #{alert_count}"
    )
    extras = sig.get("all_candidates", [])
    if extras:
        msg += "\n\n*Also triggered:*\n" + "\n".join(f"  + {s}" for s in extras)
    return msg

def format_premarket_message(vix, key_levels, events):
    date    = datetime.date.today().strftime("%A, %B %d, %Y")
    vix_str = f"{vix:.1f}" if vix else "N/A"
    levels_str = "".join(f"\n  {name}: {val:,.2f}" for name, val in key_levels.items() if val)
    events_str = "None"
    if events:
        events_str = "\n".join(f"  {e['time'].strftime('%I:%M %p ET')} - {e['name']}" for e in events)
    return (
        f"*SPX PRE-MARKET BRIEF*\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"*{date}*\n*VIX:* {vix_str}\n\n"
        f"*Key Levels:*{levels_str}\n\n"
        f"*High-Impact Events:*\n{events_str}\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"Scanning 9:30 AM - 3:30 PM ET\n"
        f"Signals: Momentum + Early Trend + Breakout + Trap + VWAP Acceptance\n"
        f"Blackout: 30min before / 1min after events"
    )

# ─────────────────────────────────────────
# TELEGRAM COMMAND POLLING
# ─────────────────────────────────────────
_last_update_id = None

def poll_telegram_commands(key_levels, today_events):
    global _last_update_id
    try:
        url    = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        params = {"timeout": 0, "limit": 10}
        if _last_update_id is not None:
            params["offset"] = _last_update_id + 1
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            return
        for update in r.json().get("result", []):
            _last_update_id = update["update_id"]
            text = update.get("message", {}).get("text", "").strip().lower()
            if text == "/brief":
                vix    = get_vix()
                events = today_events or get_economic_events()
                send_telegram(format_premarket_message(vix, key_levels, events))
                print("[COMMAND] /brief sent")
            elif text == "/status":
                now_str = datetime.datetime.now(ET).strftime("%I:%M %p ET")
                vix     = get_vix()
                vix_str = f"{vix:.1f}" if vix else "N/A"
                send_telegram(
                    f"\u2705 *Bot Status*\nTime: {now_str}\nVIX: {vix_str}\n"
                    f"Market open: {'Yes' if is_market_open() else 'No'}"
                )
    except Exception as e:
        print(f"[WARN] Command poll error: {e}")

# ─────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────
def main():
    print("=" * 50)
    print("  SPX 0DTE Signal Bot v5 - Momentum-First")
    print("=" * 50)

    alert_count       = 0
    last_signal_time  = None
    last_signal_price = None
    last_signal_bias  = None
    premarket_sent    = False
    last_date         = None
    today_events      = []
    key_levels        = {}
    last_heartbeat    = None
    last_vix          = None
    last_vix_spike    = None
    or_high           = None
    or_low            = None
    or_set            = False
    vwap_history      = []
    last_bar_time     = None
    levels_loaded     = False
    # Two-speed system state
    active_setup         = None    # current setup object or None
    setup_count          = 0       # daily setup alert counter
    last_setup_type_time = {}      # {setup_type: datetime} — prevents spam
    synthetic_ticks      = []      # rolling list of (ts, price) for fast-poll bars
    trigger_fired        = False   # prevent duplicate trigger for same setup

    while True:
        now_et = datetime.datetime.now(ET)
        today  = now_et.date()

        poll_telegram_commands(key_levels, today_events)

        if last_date != today:
            alert_count       = 0
            last_signal_time  = None
            last_signal_price = None
            last_signal_bias  = None
            premarket_sent    = False
            last_date         = today
            today_events      = []
            key_levels        = {}
            last_heartbeat    = None
            last_vix          = None
            last_vix_spike    = None
            or_high           = None
            or_low            = None
            or_set            = False
            vwap_history      = []
            last_bar_time     = None
            levels_loaded     = False
            active_setup         = None
            setup_count          = 0
            last_setup_type_time = {}
            synthetic_ticks      = []
            trigger_fired        = False
            print(f"\n[{now_et.strftime('%H:%M ET')}] New day - reset.")

        if is_premarket() and not premarket_sent and now_et.hour >= 6:
            print(f"[{now_et.strftime('%H:%M ET')}] Building pre-market brief...")
            vix          = get_vix()
            today_events = get_economic_events()
            prev         = get_prev_day_levels()
            if prev:
                key_levels = {
                    "Prev Day High":  prev["pdh"],
                    "Prev Day Low":   prev["pdl"],
                    "Prev Day Close": prev["pdc"],
                }
            try:
                import importlib, levels as lvl_mod
                importlib.reload(lvl_mod)
                raw = lvl_mod.MANUAL_LEVELS
                parsed_manual = parse_levels(raw)
                for name, meta in parsed_manual.items():
                    key_levels[name] = meta["price"]  # flat price for existing engines
                levels_loaded = True
                print(f"[LEVELS] {len(parsed_manual)} manual levels loaded from levels.py")
            except Exception as e:
                levels_loaded = False
                print(f"[ERROR] levels.py failed to load: {e}")
                send_telegram(
                    f"\u26a0\ufe0f *LEVELS LOAD FAILURE*\n"
                    f"levels.py could not be loaded: {str(e)[:100]}\n"
                    f"Signal generation is DISABLED for this session.\n"
                    f"Upload a valid levels.py and redeploy to re-enable."
                )
            bars_pm = get_spx_bars(limit=30)
            if bars_pm:
                spot_pm     = bars_pm[-1]["c"]
                round_below = (spot_pm // 100) * 100
                round_above = round_below + 100
                key_levels[f"Round {round_below:.0f}"] = round_below
                key_levels[f"Round {round_above:.0f}"] = round_above
                for offset in [-50, -25, 25, 50]:
                    lvl  = round((spot_pm + offset) / 25) * 25
                    name = f"R{lvl:.0f}"
                    if lvl % 100 != 0 and name not in key_levels:
                        key_levels[name] = float(lvl)
                pm_bars = [b for b in bars_pm if b.get("t") and
                           datetime.datetime.fromtimestamp(b["t"]/1000, ET).time() < datetime.time(9, 30)]
                if pm_bars:
                    key_levels["PM High"] = round(max(b["h"] for b in pm_bars), 2)
                    key_levels["PM Low"]  = round(min(b["l"] for b in pm_bars), 2)
            send_telegram(format_premarket_message(vix, key_levels, today_events))
            print(f"[TELEGRAM SENT] Pre-market brief")
            premarket_sent = True

        if is_market_open():
            in_blackout, event_name, mins_away, when = check_event_blackout(today_events)
            if in_blackout:
                print(f"[{now_et.strftime('%H:%M ET')}] BLACKOUT - {event_name} ({mins_away}min {when})")
                time.sleep(POLL_INTERVAL_SEC)
                continue

            bars = get_spx_bars(limit=80)
            if not bars or len(bars) < 2:
                print(f"[{now_et.strftime('%H:%M ET')}] Insufficient bars ({len(bars) if bars else 0}) — skipping.")
                time.sleep(POLL_INTERVAL_SEC)
                continue
            if len(bars) < 10:
                print(f"[{now_et.strftime('%H:%M ET')}] Warming up ({len(bars)} bars).")
                time.sleep(POLL_INTERVAL_SEC)
                continue

            spot    = bars[-1]["c"]
            vix     = get_vix()
            session = get_session(now_et)

            completed_bar_t = bars[-2]["t"] if len(bars) >= 2 else None
            new_bar = completed_bar_t != last_bar_time

            closed_bars = bars[:-1]
            closed_vwap = calc_vwap(closed_bars)

            if closed_bars:
                last_bar_dt = datetime.datetime.fromtimestamp(
                    closed_bars[-1]["t"] / 1000, ET
                ).strftime("%H:%M ET") if closed_bars[-1].get("t") else "no-ts"
                print(f"  [BAR] last completed bar: {last_bar_dt} close={closed_bars[-1]['c']:,.2f}")

            if closed_bars and closed_bars[-1].get("t"):
                last_bar_ts = datetime.datetime.fromtimestamp(closed_bars[-1]["t"] / 1000, ET)
                bar_age     = (now_et - last_bar_ts).total_seconds()
                if bar_age > 180:
                    print(f"[WARN] Data stale ({int(bar_age)}s old) — skipping signal evaluation")
                    time.sleep(POLL_INTERVAL_SEC)
                    continue

            if new_bar:
                last_bar_time = completed_bar_t
                if closed_vwap is not None:
                    vwap_history.append(closed_vwap)
                    if len(vwap_history) > 60:
                        vwap_history.pop(0)

            if closed_vwap:
                key_levels["VWAP"] = closed_vwap

            spot    = closed_bars[-1]["c"] if closed_bars else bars[-1]["c"]
            vix_str = f"{vix:.1f}" if vix else "N/A"
            print(f"[{now_et.strftime('%H:%M ET')}] SPX={spot:,.2f} VWAP={closed_vwap} VIX={vix_str} session={session} new_bar={new_bar}")

            or_start = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
            or_end   = now_et.replace(hour=9, minute=45, second=0, microsecond=0)
            if now_et < or_end:
                rth_bars = [b for b in bars if b.get("t") and
                            datetime.datetime.fromtimestamp(b["t"]/1000, ET) >= or_start]
                if rth_bars:
                    or_high = max(b["h"] for b in rth_bars)
                    or_low  = min(b["l"] for b in rth_bars)
                print(f"  -> Building OR: {or_low} - {or_high}")
            elif not or_set and or_high and or_low:
                or_set = True
                key_levels["OR High"] = round(or_high, 2)
                key_levels["OR Low"]  = round(or_low, 2)
                print(f"  -> OR locked: {or_low:.2f} - {or_high:.2f}")
                send_telegram(
                    f"\U0001f4ca *Opening Range Set*\nHigh: {or_high:,.2f}\nLow: {or_low:,.2f}\n"
                    f"Scanning for momentum + breakouts + traps..."
                )

            if vix and last_vix:
                vix_chg = (vix - last_vix) / last_vix * 100
                if vix_chg >= 8 and last_vix_spike != round(vix, 1):
                    last_vix_spike = round(vix, 1)
                    send_telegram(
                        f"\u26a0\ufe0f *VIX SPIKE*\nVIX {last_vix:.1f} -> {vix:.1f} ({vix_chg:+.1f}%)\n"
                        f"SPX={spot:,.2f}\nWait for direction before entry"
                    )
            if vix:
                last_vix = vix

            if last_heartbeat is None or (now_et - last_heartbeat).total_seconds() >= 3600:
                last_heartbeat = now_et
                send_telegram(
                    f"\U0001f499 *Bot Heartbeat* - {now_et.strftime('%I:%M %p ET')}\n"
                    f"SPX={spot:,.2f} | VWAP={closed_vwap} | VIX={vix_str}\n"
                    f"Alerts today: {alert_count}"
                )

            global _telegram_thread
            if not _telegram_thread.is_alive():
                print("[WARN] Telegram thread dead - restarting")
                _telegram_thread = threading.Thread(target=_telegram_worker, daemon=True)
                _telegram_thread.start()

            if new_bar and now_et.time() <= datetime.time(15, 30):
                if not levels_loaded:
                    print(f"  -> Signal generation DISABLED — levels.py failed to load")
                elif len(closed_bars) < 20:
                    print(f"  -> Warming up ({len(closed_bars)}/20 closed bars)")
                else:
                    # ── TWO-SPEED: CONTEXT + SETUP DETECTION (1-min bars) ─────
                    if now_et.time() >= datetime.time(9, 45):
                        ctx = detect_context(closed_bars, closed_vwap, vwap_history, key_levels)
                        print(f"  [CTX] bias={ctx['bias']} regime={ctx['regime']} location={ctx['location']} near={ctx['near_level']}")

                        # Setup lifecycle: clear if bias flipped across VWAP
                        if active_setup and closed_vwap:
                            setup_bias  = active_setup["bias"]
                            spot_closed = closed_bars[-1]["c"]
                            if setup_bias == "BULL" and spot_closed < closed_vwap:
                                print(f"  [SETUP] cleared — price crossed below VWAP")
                                active_setup = None; trigger_fired = False
                            elif setup_bias == "BEAR" and spot_closed > closed_vwap:
                                print(f"  [SETUP] cleared — price crossed above VWAP")
                                active_setup = None; trigger_fired = False

                        if setup_count < MAX_SETUPS_PER_DAY and not active_setup:
                            new_setup = detect_setup(ctx, closed_bars, closed_vwap, vwap_history, key_levels,
                                                     or_high=or_high, or_low=or_low)
                            if new_setup:
                                # Deduplicate on (type, bias, level) key
                                setup_key  = (new_setup["type"], new_setup["bias"], new_setup.get("level"))
                                last_fired = last_setup_type_time.get(setup_key)
                                if not last_fired or (now_et - last_fired).total_seconds() > 1200:
                                    new_setup["start_time"] = now_et
                                    active_setup  = new_setup
                                    trigger_fired = False
                                    synthetic_ticks = []
                                    setup_count  += 1
                                    last_setup_type_time[setup_key] = now_et
                                    send_telegram(format_setup_message(new_setup, or_high=or_high, or_low=or_low))
                                    print(f"  [SETUP] {new_setup['type']} | {new_setup['bias']} | {new_setup['message']}")
                                else:
                                    mins = int((now_et - last_fired).total_seconds() / 60)
                                    print(f"  [SETUP] suppressed — same setup fired {mins}min ago")

                    # ── EXISTING SIGNAL ENGINES (unchanged) ───────────────────
                    sig = evaluate_signal(
                        closed_bars, key_levels, closed_vwap, vwap_history, vix, session,
                        last_signal_time, last_signal_price, last_signal_bias, or_set,
                        or_high=or_high, or_low=or_low
                    )
                    if sig:
                        alert_count      += 1
                        last_signal_time  = now_et
                        last_signal_price = sig["spot"]
                        last_signal_bias  = sig["bias"]
                        msg = format_signal_message(sig, alert_count)
                        send_telegram(msg)
                        log_signal(sig)
                        print(f"  -> SIGNAL [{sig['signal_type']}]: {sig['trigger']} | {sig['bias']} | score={sig['score']}")
                    else:
                        print(f"  -> No signal this bar")

            elif not new_bar:
                print(f"  -> Same bar - skipping")

            # ── FAST MODE — runs every 15s independently of bar gate ──────────
            # This path is always evaluated when a setup is active,
            # regardless of whether a new 1-minute bar printed.
            if active_setup and not trigger_fired and now_et.time() <= datetime.time(15, 30):

                setup_start = active_setup.get("start_time")
                atr_val     = calc_atr(closed_bars) if closed_bars else 5.0

                # Lifecycle: expire setup after 10 minutes
                if setup_start and (now_et - setup_start).total_seconds() > 600:
                    print(f"  [FAST] setup expired after 10 min — clearing")
                    active_setup = None
                    trigger_fired = False

                else:
                    live_price = get_spx_live_price()
                    if live_price:
                        level_price  = active_setup.get("level_price")
                        setup_origin = active_setup.get("spot", live_price)

                        # Lifecycle: clear if bias flipped across VWAP (fast mode check)
                        if closed_vwap and active_setup:
                            setup_bias = active_setup["bias"]
                            if setup_bias == "BULL" and live_price < closed_vwap:
                                print(f"  [FAST] setup cleared — live price crossed below VWAP")
                                active_setup = None; trigger_fired = False
                            elif setup_bias == "BEAR" and live_price > closed_vwap:
                                print(f"  [FAST] setup cleared — live price crossed above VWAP")
                                active_setup = None; trigger_fired = False

                        # Lifecycle: clear if price moves >20pts from setup level
                        if level_price and abs(live_price - level_price) > 20:
                            print(f"  [FAST] setup cleared — price {live_price:,.2f} drifted >20pts from {level_price:,.2f}")
                            active_setup = None; trigger_fired = False

                        # Anti-chase pre-check before adding ticks
                        elif closed_vwap and abs(live_price - closed_vwap) > 15:
                            print(f"  [FAST] anti-chase: {abs(live_price - closed_vwap):.1f}pts from VWAP — not collecting ticks")

                        elif atr_val and abs(live_price - setup_origin) > atr_val * 1.5:
                            print(f"  [FAST] anti-chase: moved {abs(live_price - setup_origin):.1f}pts from origin — clearing setup")
                            active_setup = None; trigger_fired = False

                        else:
                            # Collect tick
                            ts = now_et.timestamp()
                            synthetic_ticks.append((ts, live_price))
                            # Keep rolling 3-minute window
                            cutoff = ts - 180
                            synthetic_ticks = [(t, p) for t, p in synthetic_ticks if t >= cutoff]

                            # Build 45-second synthetic bars (need 3+ ticks per bar for reliable wicks)
                            synth_bars  = []
                            bucket_size = 45  # seconds
                            if len(synthetic_ticks) >= 2:
                                start_ts = synthetic_ticks[0][0]
                                bucket   = []
                                for tick_ts, tick_p in synthetic_ticks:
                                    if tick_ts - start_ts < bucket_size:
                                        bucket.append((tick_ts, tick_p))
                                    else:
                                        bar = build_synthetic_bar(bucket)
                                        if bar:
                                            synth_bars.append(bar)
                                        bucket   = [(tick_ts, tick_p)]
                                        start_ts = tick_ts
                                if bucket:
                                    bar = build_synthetic_bar(bucket)
                                    if bar:
                                        synth_bars.append(bar)

                            print(f"  [FAST] live={live_price:,.2f} ticks={len(synthetic_ticks)} synth_bars={len(synth_bars)}")

                            if len(synth_bars) >= 3 or len(synthetic_ticks) >= 4:
                                trig = detect_fast_trigger(
                                    synth_bars, active_setup, atr_val, closed_vwap,
                                    setup_start_time=setup_start, now_et=now_et,
                                    synthetic_ticks=synthetic_ticks
                                )
                                if trig:
                                    # Store setup before clearing
                                    trigger_setup = active_setup
                                    trigger_fired = True
                                    active_setup  = None
                                    synthetic_ticks = []
                                    alert_count  += 1
                                    msg = format_trigger_message(
                                        trig, trigger_setup, closed_bars,
                                        closed_vwap, vix, session, atr_val
                                    )
                                    send_telegram(msg)
                                    print(f"  [TRIGGER] {trig['setup_type']} | {trig['bias']} | mode={trig.get('trigger_mode','?')} | net={trig['net3']:+.1f}pts @ {live_price:,.2f}")


        else:
            print(f"[{now_et.strftime('%H:%M ET')}] Market closed.")

        time.sleep(POLL_INTERVAL_SEC)

# ─────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────
app = Flask(__name__)

@app.route("/health")
def health():
    return {"status": "ok", "bot": "SPX 0DTE Signal Bot v5"}, 200

@app.route("/")
def index():
    return {"status": "running"}, 200

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

if __name__ == "__main__":
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    print(f"[HEALTH] Server started on port {os.environ.get('PORT', 8080)}")
    while True:
        try:
            main()
        except Exception as e:
            print(f"[CRITICAL] Crashed: {e} - restarting in 30s")
            send_telegram(f"\u26a0\ufe0f *Bot Crashed*\n{str(e)[:100]}\nRestarting in 30s...")
            time.sleep(30)
