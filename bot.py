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
MAX_ALERTS_PER_DAY = 20

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

# Breakout engine thresholds
BREAKOUT_FOLLOW_THROUGH_MIN = 2.0
BREAKOUT_RETEST_WINDOW      = 8.0
RETEST_LOOKBACK             = 12
BREAKOUT_MIN_MOMENTUM       = 3.0

# Shared
MIN_SIGNAL_DISTANCE = 4.0

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
                results = list(reversed(results))  # desc → chronological order
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

def evaluate_momentum_signal(bars, vwap, vwap_history, vix, session):
    """
    Primary engine. Fires on impulse price moves regardless of key levels.
    Scoring up to ~17 points. Min score: MORNING=8, MIDDAY=7, AFTERNOON=6.
    """
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

    # 3-bar impulse (always true here)
    score += 3
    reasons.append(f"3-bar {mom3:+.1f}pts")

    # 5-bar impulse
    if (direction == "BULL" and mom5 >= min5) or (direction == "BEAR" and mom5 <= -min5):
        score += 2
        reasons.append(f"5-bar {mom5:+.1f}pts")

    # Range expansion
    latest_range = latest["h"] - latest["l"]
    avg_range    = sum(b["h"] - b["l"] for b in bars[-5:-1]) / 4 if len(bars) >= 5 else latest_range

    # Minimum bar size filter — reject low-energy candles relative to ATR
    if latest_range < atr * 0.20:
        print(f"  [MOM] Rejected: bar range {latest_range:.1f} < ATR*0.20 ({atr * 0.20:.1f})")
        return None

    if avg_range > 0 and latest_range >= avg_range * MOMENTUM_BAR_RANGE_EXPANSION:
        score += 2
        reasons.append(f"range expansion")

    # Close strength in last 3 bars
    strong_closes = sum(1 for b in recent3 if _close_strength(b, direction))
    if strong_closes >= 2:
        score += 2
        reasons.append(f"{strong_closes}/3 strong closes")
    elif strong_closes == 1:
        score += 1

    # VWAP alignment
    above_vwap = vwap and spot > vwap
    below_vwap = vwap and spot < vwap
    if (direction == "BULL" and above_vwap) or (direction == "BEAR" and below_vwap):
        score += 2
        reasons.append("VWAP aligned")
    elif vwap is None:
        score += 1

    # VWAP slope
    vwap_slope = calc_vwap_slope(vwap_history, n=5)
    if (direction == "BULL" and vwap_slope > 0.15) or (direction == "BEAR" and vwap_slope < -0.15):
        score += 2
        reasons.append(f"VWAP slope {vwap_slope:+.2f}")

    # Clean sequence (few opposite-color bars)
    if direction == "BULL":
        opp = sum(1 for b in recent5 if b["c"] < b["o"])
    else:
        opp = sum(1 for b in recent5 if b["c"] > b["o"])
    if opp <= MOMENTUM_MAX_OPPOSITE_BARS:
        score += 1
        reasons.append(f"clean ({opp} opp bars)")

    # Wick clean on latest bar
    if _wick_ok(latest, direction):
        score += 1
        reasons.append("wick ok")

    # Acceleration bonus
    if len(bars) >= 7:
        prior_mom3 = bars[-4]["c"] - bars[-7]["c"]
        if (direction == "BULL" and mom3 > prior_mom3 > 0) or (direction == "BEAR" and mom3 < prior_mom3 < 0):
            score += 2
            reasons.append(f"accelerating")

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
    """
    Secondary engine. Fires on confirmed key level breaks and retests.
    OR levels suppressed until or_set=True.
    """
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

        # BULL BREAKOUT
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

        # BULL RETEST
        elif level < spot <= level + BREAKOUT_RETEST_WINDOW:
            was_above = any(b["c"] > level + follow for b in bars[-RETEST_LOOKBACK:-3])
            if was_above and mom3 >= 0 and above_vwap:
                candidates.append({
                    "bias": "BULL", "signal_type": "RETEST",
                    "trigger": f"Retest hold {level_name} ({level:,.0f})",
                    "level": level, "level_name": level_name, "score": 7,
                })

        # BEAR BREAKDOWN
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

        # BEAR RETEST
        elif level - BREAKOUT_RETEST_WINDOW <= spot < level:
            was_below = any(b["c"] < level - follow for b in bars[-RETEST_LOOKBACK:-3])
            if was_below and mom3 <= 0 and below_vwap:
                candidates.append({
                    "bias": "BEAR", "signal_type": "RETEST",
                    "trigger": f"Retest fail {level_name} ({level:,.0f})",
                    "level": level, "level_name": level_name, "score": 7,
                })

    # VWAP reclaim / rejection
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
# COMBINED EVALUATOR
# ─────────────────────────────────────────
def evaluate_signal(bars, key_levels, vwap, vwap_history, vix, session,
                    last_signal_time, last_signal_price, last_signal_bias, or_set):
    now_et = datetime.datetime.now(ET)

    if last_signal_time:
        if (now_et - last_signal_time).total_seconds() / 60 < COOLDOWN_MINUTES:
            return None

    mom_sig = evaluate_momentum_signal(bars, vwap, vwap_history, vix, session)
    brk_sig = evaluate_breakout_signal(bars, key_levels, vwap, vix, session, or_set)

    if mom_sig and brk_sig:
        best = mom_sig if mom_sig["score"] >= brk_sig["score"] - 1 else brk_sig
        winner = "momentum" if best is mom_sig else "breakout"
        print(f"  -> {winner} wins (mom={mom_sig['score']} brk={brk_sig['score']})")
    elif mom_sig:
        best = mom_sig
    elif brk_sig:
        best = brk_sig
    else:
        return None

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

def format_signal_message(sig, alert_count, max_alerts):
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
        f"Alert {alert_count}/{max_alerts}"
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
        f"Signals: Momentum impulse (primary) + Key level breakouts (secondary)\n"
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

            # ── Manual key levels for today Apr 21 2026 ──────────────────────
            # Source: John's level map + SPX VWAP chart
            key_levels["Daily Pivot 7110"]    = 7110.0   # Bull/Bear pivot — critical
            key_levels["R1 7122"]             = 7122.0   # First resistance
            key_levels["R2 7135"]             = 7135.0
            key_levels["R3 7147"]             = 7147.0
            key_levels["Daily 1SD Upper 7152"]= 7152.0   # Upside target
            key_levels["S1 7097"]             = 7097.0   # First support
            key_levels["S2 7085"]             = 7085.0
            key_levels["Daily 1SD Lower 7066"]= 7066.0   # Bear target
            key_levels["WTD VWAP 7109"]       = 7109.0   # Triple confluence w/ PDC + pivot
            key_levels["Daily VWAP 7088"]     = 7088.0   # Key support if 7110 breaks
            key_levels["Round 7100"]          = 7100.0
            key_levels["Round 7200"]          = 7200.0
            # ─────────────────────────────────────────────────────────────────
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

            if alert_count >= MAX_ALERTS_PER_DAY:
                print(f"[{now_et.strftime('%H:%M ET')}] Max alerts reached.")
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
            vix_str = f"{vix:.1f}" if vix else "N/A"  # temporary; overwritten below

            # Once-per-completed-bar gate
            # bars[-1] is the live unfinished bar; bars[-2] is the last completed bar
            completed_bar_t = bars[-2]["t"] if len(bars) >= 2 else None
            new_bar = completed_bar_t != last_bar_time

            # All signal logic uses completed bars only
            closed_bars = bars[:-1]
            closed_vwap = calc_vwap(closed_bars)

            # Debug: confirm last completed bar is current, not stale
            if closed_bars:
                last_bar_dt = datetime.datetime.fromtimestamp(
                    closed_bars[-1]["t"] / 1000, ET
                ).strftime("%H:%M ET") if closed_bars[-1].get("t") else "no-ts"
                print(f"  [BAR] last completed bar: {last_bar_dt} close={closed_bars[-1]['c']:,.2f}")

            # Stale data guard — skip signal eval if last completed bar is too old
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

            # Opening range (9:30-9:45) — build in background, do NOT skip signal eval
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
                    f"Scanning for momentum + breakouts..."
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
                    f"Alerts today: {alert_count}/{MAX_ALERTS_PER_DAY}"
                )

            global _telegram_thread
            if not _telegram_thread.is_alive():
                print("[WARN] Telegram thread dead - restarting")
                _telegram_thread = threading.Thread(target=_telegram_worker, daemon=True)
                _telegram_thread.start()

            # Signal eval: only on new completed bar, within entry window
            if new_bar and now_et.time() <= datetime.time(15, 30):
                if len(closed_bars) < 20:
                    print(f"  -> Warming up ({len(closed_bars)}/20 closed bars)")
                else:
                    sig = evaluate_signal(
                        closed_bars, key_levels, closed_vwap, vwap_history, vix, session,
                        last_signal_time, last_signal_price, last_signal_bias, or_set
                    )
                    if sig:
                        alert_count      += 1
                        last_signal_time  = now_et
                        last_signal_price = sig["spot"]
                        last_signal_bias  = sig["bias"]
                        msg = format_signal_message(sig, alert_count, MAX_ALERTS_PER_DAY)
                        send_telegram(msg)
                        log_signal(sig)
                        print(f"  -> SIGNAL [{sig['signal_type']}]: {sig['trigger']} | {sig['bias']} | score={sig['score']}")
                    else:
                        print(f"  -> No signal this bar")
            elif not new_bar:
                print(f"  -> Same bar - skipping")

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
