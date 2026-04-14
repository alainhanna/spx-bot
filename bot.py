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
COOLDOWN_MINUTES   = 2          # minimum minutes between signals
MAX_ALERTS_PER_DAY = 20

# Breakout engine config — session-aware
BREAKOUT_CONFIRM_BARS  = 1      # MORNING: 1 bar confirm; MIDDAY/AFTERNOON: 2 bars
BREAKOUT_MIN_MOMENTUM  = 4.0   # MORNING; MIDDAY=2.5, AFTERNOON=2.0 (applied in engine)
BREAKOUT_RETEST_WINDOW = 8.0   # points — how close to level counts as retest
RETEST_LOOKBACK        = 12    # bars to look back for prior breakout confirmation
PULLBACK_MAX           = 5.0   # points — max pullback allowed on retest entry
FOLLOW_THROUGH_MIN     = 2.0   # points past level required after break bar

# Profit targets / stops (option premium %)
PROFIT_TARGET_1_PCT = 40
PROFIT_TARGET_2_PCT = 80
STOP_LOSS_PCT       = 50

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
    """Fetch SPX 1-min bars from Polygon. Returns bars sorted oldest→newest."""
    today = datetime.datetime.now(ET).date().isoformat()
    url = (
        f"https://api.polygon.io/v2/aggs/ticker/I:SPX/range/1/minute/{today}/{today}"
        f"?adjusted=true&sort=asc&limit={limit}&apiKey={POLYGON_API_KEY}"
    )
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 429:
                time.sleep(2 ** (attempt + 1))
                continue
            if r.status_code != 200:
                raise Exception(f"HTTP {r.status_code}")
            data = r.json()
            results = data.get("results", [])
            if results:
                print(f"[POLYGON] {len(results)} bars fetched")
                return results
            else:
                print(f"[WARN] Polygon returned 0 bars. resultsCount={data.get('resultsCount')} status={data.get('status')}")
        except Exception as e:
            if attempt < 2:
                print(f"[WARN] Polygon bars attempt {attempt+1} failed ({e}), retrying...")
                time.sleep(2 ** (attempt + 1))
            else:
                print(f"[WARN] Polygon bars failed after 3 attempts")
    return []

_vix_cache = {"value": None, "ts": None}

def get_vix():
    """Fetch live VIX from Polygon snapshot endpoint (not /prev)."""
    now = datetime.datetime.now(ET)
    if _vix_cache["ts"] and (now - _vix_cache["ts"]).total_seconds() < 60:
        return _vix_cache["value"]
    for attempt in range(3):
        try:
            # Use snapshot for live value during market hours
            url = f"https://api.polygon.io/v3/snapshot?ticker.any_of=I:VIX&apiKey={POLYGON_API_KEY}"
            r = requests.get(url, timeout=10)
            if r.status_code == 429:
                time.sleep(2 ** (attempt + 1))
                continue
            if r.status_code == 200:
                data = r.json()
                results = data.get("results", [])
                if results:
                    val = float(results[0].get("session", {}).get("close") or results[0].get("value", 0))
                    if val > 0:
                        _vix_cache["value"] = round(val, 2)
                        _vix_cache["ts"] = now
                        return _vix_cache["value"]
            # Fallback: /prev for last close
            url2 = f"https://api.polygon.io/v2/aggs/ticker/I:VIX/prev?apiKey={POLYGON_API_KEY}"
            r2 = requests.get(url2, timeout=10)
            if r2.status_code == 200:
                data2 = r2.json()
                if data2.get("results"):
                    val = float(data2["results"][0]["c"])
                    _vix_cache["value"] = round(val, 2)
                    _vix_cache["ts"] = now
                    return _vix_cache["value"]
        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
            else:
                print(f"[WARN] VIX unavailable: {e}")
    return _vix_cache["value"]

def get_prev_day_levels():
    """Get prior trading day OHLC."""
    for days_back in range(1, 6):
        try:
            d = (datetime.date.today() - datetime.timedelta(days=days_back)).isoformat()
            url = (
                f"https://api.polygon.io/v2/aggs/ticker/I:SPX/range/1/day/{d}/{d}"
                f"?adjusted=true&apiKey={POLYGON_API_KEY}"
            )
            r = requests.get(url, timeout=10)
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
    """
    VWAP from RTH bars only. Bars must have 't' timestamp in milliseconds.
    Uses strict ET 9:30 filter.
    """
    rth_bars = []
    for b in bars:
        t = b.get("t")
        if t:
            bar_dt = datetime.datetime.fromtimestamp(t / 1000, ET)
            if bar_dt.hour > 9 or (bar_dt.hour == 9 and bar_dt.minute >= 30):
                rth_bars.append(b)
    if not rth_bars:
        print(f"[VWAP] No RTH bars found from {len(bars)} total bars")
        return None
    tp_vol = sum(((b["h"] + b["l"] + b["c"]) / 3) * b.get("v", 1) for b in rth_bars)
    vol    = sum(b.get("v", 1) for b in rth_bars)
    vwap   = round(tp_vol / vol, 2) if vol else None
    print(f"[VWAP] Computed from {len(rth_bars)} RTH bars → {vwap}")
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

def get_regime(vix, bars):
    """Simple regime: VIX-based only. CHOP detected separately per signal."""
    if vix and vix >= 30:
        return "HIGH_VOL"
    elif vix and vix >= 20:
        return "ELEVATED"
    return "NORMAL"

# ─────────────────────────────────────────
# ECONOMIC CALENDAR
# ─────────────────────────────────────────
def get_economic_events():
    today  = datetime.date.today()
    events = []
    # Add manual events here each week
    MANUAL_EVENTS = [
        # ("08:30", "CPI"),
        # ("14:00", "FOMC"),
    ]
    for time_str, name in MANUAL_EVENTS:
        try:
            h, m = map(int, time_str.split(":"))
            dt = ET.localize(datetime.datetime(today.year, today.month, today.day, h, m))
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
# BREAKOUT SIGNAL ENGINE
# ─────────────────────────────────────────
def bars_above_level(bars, level, n):
    """Check if last n bars all closed above level."""
    if len(bars) < n:
        return False
    return all(b["c"] > level for b in bars[-n:])

def bars_below_level(bars, level, n):
    """Check if last n bars all closed below level."""
    if len(bars) < n:
        return False
    return all(b["c"] < level for b in bars[-n:])

def is_chop(bars):
    """Detect choppy price action: too many direction changes, tight range."""
    if len(bars) < 10:
        return False
    recent = bars[-10:]
    hi = max(b["h"] for b in recent)
    lo = min(b["l"] for b in recent)
    range_pts = hi - lo
    changes = sum(
        1 for i in range(2, len(recent))
        if (recent[i]["c"] - recent[i-1]["c"]) * (recent[i-1]["c"] - recent[i-2]["c"]) < 0
    )
    return range_pts < 8 or changes >= 6

def evaluate_breakout(bars, key_levels, vwap, vix, session,
                      last_signal_time, last_signal_price, last_signal_bias):
    """
    Pure breakout signal engine.

    Signal fires when:
    1. SPX breaks a key level (OR High/Low, PDH/PDL, round number, PM High/Low)
    2. BREAKOUT_CONFIRM_BARS consecutive bars close beyond the level
    3. Momentum >= BREAKOUT_MIN_MOMENTUM in the break direction
    4. Not in chop
    5. VWAP is on the same side (confirming, not filtering)
    6. Cooldown and distance filters pass

    Also fires on RETEST: price breaks, pulls back to the level, holds.
    """
    if len(bars) < 10:
        return None

    spot     = bars[-1]["c"]
    momentum = calc_momentum(bars, 3)
    now_et   = datetime.datetime.now(ET)
    regime   = get_regime(vix, bars)

    # Session-aware thresholds
    if session == "MORNING":
        confirm_bars  = 1
        min_momentum  = 4.0
    elif session == "MIDDAY":
        confirm_bars  = 2
        min_momentum  = 2.5
    else:  # AFTERNOON
        confirm_bars  = 2
        min_momentum  = 2.0

    # Cooldown check
    if last_signal_time:
        mins_since = (now_et - last_signal_time).total_seconds() / 60
        if mins_since < COOLDOWN_MINUTES:
            return None

    # Chop filter
    if is_chop(bars):
        print(f"  → Chop detected — no signal")
        return None

    # VWAP side (confirming only — not blocking)
    above_vwap = vwap and spot > vwap
    below_vwap = vwap and spot < vwap

    candidates = []

    for level_name, level in key_levels.items():
        if level is None:
            continue

        # ── BULL BREAKOUT ────────────────────────────────────
        if spot > level + FOLLOW_THROUGH_MIN:
            if bars_above_level(bars, level, confirm_bars):
                if momentum >= min_momentum:
                    vwap_confirms = above_vwap
                    score = 8
                    if vwap_confirms:
                        score += 2
                    if "OR" in level_name:
                        score += 1
                    if "Prev Day High" in level_name:
                        score += 1
                    if "Round" in level_name:
                        score += 1
                    candidates.append({
                        "bias":       "BULL",
                        "trigger":    f"Breakout above {level_name} ({level:,.0f})",
                        "level":      level,
                        "level_name": level_name,
                        "score":      score,
                        "type":       "breakout",
                    })
                    print(f"  → BULL breakout candidate: {level_name} score={score} mom={momentum:+.1f}")

        # ── BULL RETEST ──────────────────────────────────────
        elif level < spot <= level + BREAKOUT_RETEST_WINDOW:
            was_above = any(b["c"] > level + FOLLOW_THROUGH_MIN for b in bars[-RETEST_LOOKBACK:-3])
            if was_above and momentum >= 0 and above_vwap:
                candidates.append({
                    "bias":       "BULL",
                    "trigger":    f"Retest hold at {level_name} ({level:,.0f})",
                    "level":      level,
                    "level_name": level_name,
                    "score":      7,
                    "type":       "retest",
                })
                print(f"  → BULL retest candidate: {level_name} mom={momentum:+.1f}")

        # ── BEAR BREAKDOWN ───────────────────────────────────
        if spot < level - FOLLOW_THROUGH_MIN:
            if bars_below_level(bars, level, confirm_bars):
                if momentum <= -min_momentum:
                    vwap_confirms = below_vwap
                    score = 8
                    if vwap_confirms:
                        score += 2
                    if "OR" in level_name:
                        score += 1
                    if "Prev Day Low" in level_name:
                        score += 1
                    if "Round" in level_name:
                        score += 1
                    candidates.append({
                        "bias":       "BEAR",
                        "trigger":    f"Breakdown below {level_name} ({level:,.0f})",
                        "level":      level,
                        "level_name": level_name,
                        "score":      score,
                        "type":       "breakdown",
                    })
                    print(f"  → BEAR breakdown candidate: {level_name} score={score} mom={momentum:+.1f}")

        # ── BEAR RETEST ──────────────────────────────────────
        elif level - BREAKOUT_RETEST_WINDOW <= spot < level:
            was_below = any(b["c"] < level - FOLLOW_THROUGH_MIN for b in bars[-RETEST_LOOKBACK:-3])
            if was_below and momentum <= 0 and below_vwap:
                candidates.append({
                    "bias":       "BEAR",
                    "trigger":    f"Retest fail at {level_name} ({level:,.0f})",
                    "level":      level,
                    "level_name": level_name,
                    "score":      7,
                    "type":       "retest",
                })
                print(f"  → BEAR retest candidate: {level_name} mom={momentum:+.1f}")

    # ── VWAP RECLAIM (BULL) ──────────────────────────────
    if vwap:
        prev_close = bars[-2]["c"] if len(bars) >= 2 else None
        if prev_close and prev_close < vwap <= spot and momentum >= min_momentum:
            if bars_above_level(bars, vwap, confirm_bars):
                candidates.append({
                    "bias":       "BULL",
                    "trigger":    f"VWAP Reclaim ({vwap:,.2f})",
                    "level":      vwap,
                    "level_name": "VWAP",
                    "score":      9,
                    "type":       "breakout",
                })
                print(f"  → VWAP Reclaim candidate mom={momentum:+.1f}")

        # ── VWAP REJECTION (BEAR) ────────────────────────────
        if prev_close and prev_close > vwap >= spot and momentum <= -min_momentum:
            if bars_below_level(bars, vwap, confirm_bars):
                candidates.append({
                    "bias":       "BEAR",
                    "trigger":    f"VWAP Rejection ({vwap:,.2f})",
                    "level":      vwap,
                    "level_name": "VWAP",
                    "score":      9,
                    "type":       "breakdown",
                })
                print(f"  → VWAP Rejection candidate mom={momentum:+.1f}")

    # ── MOMENTUM SURGE (standalone) ──────────────────────
    # 6+ pts in 3 bars, expanding ranges, no level nearby required
    surge_threshold = 6.0
    if abs(momentum) >= surge_threshold:
        if len(bars) >= 3:
            ranges   = [b["h"] - b["l"] for b in bars[-3:]]
            expanding = ranges[-1] > ranges[0]
            if expanding:
                bias  = "BULL" if momentum > 0 else "BEAR"
                vwap_ok = (bias == "BULL" and above_vwap) or (bias == "BEAR" and below_vwap) or not vwap
                if vwap_ok and bars_above_level(bars, spot - 1, confirm_bars) if bias == "BULL" else bars_below_level(bars, spot + 1, confirm_bars):
                    near_existing = any(
                        v is not None and abs(spot - v) <= FOLLOW_THROUGH_MIN
                        for k, v in key_levels.items() if k != "VWAP"
                    )
                    # Only fire surge if NOT already captured by a level breakout
                    if not near_existing:
                        candidates.append({
                            "bias":       bias,
                            "trigger":    f"Momentum surge ({momentum:+.1f} pts)",
                            "level":      spot,
                            "level_name": "Surge",
                            "score":      8,
                            "type":       "surge",
                        })
                        print(f"  → Momentum surge candidate: {momentum:+.1f}pts expanding={expanding}")

    if not candidates:
        return None

    # Pick highest score, tiebreak: breakout > retest
    candidates.sort(key=lambda x: (x["score"], 0 if x["type"] == "breakout" else -1), reverse=True)
    best = candidates[0]

    # Distance filter vs last signal (same direction)
    if last_signal_price and last_signal_bias == best["bias"]:
        dist = abs(spot - last_signal_price)
        if dist < 6:
            print(f"  → Distance filter: {dist:.1f}pts from last {last_signal_bias} signal")
            return None

    # Afternoon OR suppression (OR breakouts only in MORNING/MIDDAY)
    if "OR" in best["level_name"] and session == "AFTERNOON":
        print(f"  → OR breakout suppressed in AFTERNOON")
        return None

    # Build exit levels
    atr = calc_atr(bars)
    if session == "MORNING":
        t1_dist, t2_dist = atr * 1.5, atr * 3.0
        stop_pct = 50
    elif session == "MIDDAY":
        t1_dist, t2_dist = atr * 1.0, atr * 2.0
        stop_pct = 45
    else:
        t1_dist, t2_dist = atr * 0.8, atr * 1.5
        stop_pct = 40

    if regime == "HIGH_VOL":
        t1_dist *= 1.3
        t2_dist *= 1.3

    if best["bias"] == "BULL":
        option_type = "CALL"
        strike      = round((spot * 1.002) / 5) * 5
        target_1    = round(spot + t1_dist, 2)
        target_2    = round(spot + t2_dist, 2)
        invalidate  = round(best["level"] - atr * 0.5, 2)
        no_chase    = round(spot + 3, 2)
    else:
        option_type = "PUT"
        strike      = round((spot * 0.998) / 5) * 5
        target_1    = round(spot - t1_dist, 2)
        target_2    = round(spot - t2_dist, 2)
        invalidate  = round(best["level"] + atr * 0.5, 2)
        no_chase    = round(spot - 3, 2)

    quality = "STRONG" if best["score"] >= 11 else "HIGH" if best["score"] >= 9 else "MEDIUM"

    return {
        "trigger":     best["trigger"],
        "type":        best["type"],
        "quality":     quality,
        "score":       best["score"],
        "regime":      regime,
        "session":     session,
        "bias":        best["bias"],
        "option_type": option_type,
        "spot":        round(spot, 2),
        "strike":      strike,
        "vwap":        vwap,
        "momentum":    momentum,
        "atr":         atr,
        "vix":         vix,
        "target_1":    target_1,
        "target_2":    target_2,
        "invalidate":  invalidate,
        "no_chase":    no_chase,
        "stop_pct":    stop_pct,
        "time_stop":   "3:45 PM ET",
        "level":       best["level"],
        "all_candidates": [c["trigger"] for c in candidates[1:4]],
    }

# ─────────────────────────────────────────
# SIGNAL LOGGING
# ─────────────────────────────────────────
SIGNAL_LOG_FILE   = "signal_log.csv"
SIGNAL_LOG_FIELDS = [
    "timestamp", "trigger", "type", "score", "quality", "regime", "session",
    "bias", "spot", "vwap", "momentum", "atr", "level",
    "target_1", "target_2", "invalidate"
]

def log_signal(sig):
    try:
        write_header = not os.path.exists(SIGNAL_LOG_FILE)
        with open(SIGNAL_LOG_FILE, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=SIGNAL_LOG_FIELDS)
            if write_header:
                writer.writeheader()
            writer.writerow({
                "timestamp": datetime.datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET"),
                "trigger":   sig.get("trigger", ""),
                "type":      sig.get("type", ""),
                "score":     sig.get("score", ""),
                "quality":   sig.get("quality", ""),
                "regime":    sig.get("regime", ""),
                "session":   sig.get("session", ""),
                "bias":      sig.get("bias", ""),
                "spot":      sig.get("spot", ""),
                "vwap":      sig.get("vwap", ""),
                "momentum":  sig.get("momentum", ""),
                "atr":       sig.get("atr", ""),
                "level":     sig.get("level", ""),
                "target_1":  sig.get("target_1", ""),
                "target_2":  sig.get("target_2", ""),
                "invalidate": sig.get("invalidate", ""),
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
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       message,
                "parse_mode": "Markdown"
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
        print(f"[TELEGRAM] Queue full — dropped")

def format_signal_message(sig, alert_count, max_alerts):
    t     = datetime.datetime.now(ET).strftime("%I:%M %p ET")
    emoji = "🟢" if sig["bias"] == "BULL" else "🔴"
    type_label = "BREAKOUT" if sig["type"] == "breakout" else "BREAKDOWN" if sig["bias"] == "BEAR" else "RETEST"

    quality_badges = {"STRONG": "⭐⭐ STRONG", "HIGH": "⭐ HIGH", "MEDIUM": "MEDIUM"}
    badge = quality_badges.get(sig["quality"], sig["quality"])

    regime_labels  = {"HIGH_VOL": "🔥 High Vol", "ELEVATED": "⚡ Elevated", "NORMAL": "✅ Normal"}
    regime_str = regime_labels.get(sig["regime"], sig["regime"])

    vix_str  = f"{sig['vix']:.1f}" if sig.get("vix") else "N/A"
    vwap_str = f"{sig['vwap']:,.2f}" if sig.get("vwap") else "N/A"

    msg = f"""*SPX 0DTE {type_label} {emoji} {sig['option_type']}*
━━━━━━━━━━━━━━━━━━━━━━
*Quality:*  {badge} (score: {sig['score']})
*Regime:*   {regime_str} | {sig['session']}
*Trigger:*  {sig['trigger']}
*Time:*     {t}
━━━━━━━━━━━━━━━━━━━━━━
*Spot:*     {sig['spot']:,.2f}
*Strike:*   {sig['strike']} {sig['option_type']} 0DTE
*VWAP:*     {vwap_str}
*Momentum:* {sig['momentum']:+.1f} pts  |  *ATR:* {sig['atr']:.1f}
*VIX:*      {vix_str}
━━━━━━━━━━━━━━━━━━━━━━
*T1:* {sig['target_1']:,.2f}  |  *T2:* {sig['target_2']:,.2f}
*Stop:* -{sig['stop_pct']}% premium  |  SPX {sig['invalidate']:,.2f}
*No chase:* past {sig['no_chase']:,.2f}
*Exit by:* {sig['time_stop']}
━━━━━━━━━━━━━━━━━━━━━━
Alert {alert_count}/{max_alerts}"""

    extras = sig.get("all_candidates", [])
    if extras:
        msg += "\n\n*Also triggered:*\n" + "\n".join(f"  + {s}" for s in extras)

    return msg

def format_premarket_message(vix, key_levels, events):
    date    = datetime.date.today().strftime("%A, %B %d, %Y")
    vix_str = f"{vix:.1f}" if vix else "N/A"

    levels_str = ""
    for name, val in key_levels.items():
        if val:
            levels_str += f"\n  {name}: {val:,.2f}"

    events_str = "None"
    if events:
        events_str = "\n".join(
            f"  {e['time'].strftime('%I:%M %p ET')} — {e['name']}" for e in events
        )

    return f"""*SPX PRE-MARKET BRIEF*
━━━━━━━━━━━━━━━━━━━━━━
*{date}*
*VIX:* {vix_str}

*Key Levels:*{levels_str}

*High-Impact Events:*
{events_str}
━━━━━━━━━━━━━━━━━━━━━━
Scanning 9:30 AM — 3:30 PM ET
Signals: Breakouts + Breakdowns at key levels with momentum confirmation
Blackout: 30min before / 1min after events"""

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
                    f"✅ *Bot Status*\nTime: {now_str}\nVIX: {vix_str}\n"
                    f"Market open: {'Yes' if is_market_open() else 'No'}"
                )
                print("[COMMAND] /status sent")
    except Exception as e:
        print(f"[WARN] Command poll error: {e}")

# ─────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────
def main():
    print("=" * 50)
    print("  SPX 0DTE Breakout Bot v4")
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

    # Opening range
    or_high = None
    or_low  = None
    or_set  = False

    # VIX spike
    last_vix       = None
    last_vix_spike = None

    while True:
        now_et = datetime.datetime.now(ET)
        today  = now_et.date()

        poll_telegram_commands(key_levels, today_events)

        # Daily reset
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
            or_high           = None
            or_low            = None
            or_set            = False
            last_vix          = None
            last_vix_spike    = None
            print(f"\n[{now_et.strftime('%H:%M ET')}] New day — reset.")

        # Pre-market brief
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
            bars_pm = get_spx_bars(limit=30)
            if bars_pm:
                spot_pm     = bars_pm[-1]["c"]
                # 100-point round numbers (one below, one above)
                round_below = (spot_pm // 100) * 100
                round_above = round_below + 100
                key_levels[f"Round {round_below:.0f}"] = round_below
                key_levels[f"Round {round_above:.0f}"] = round_above
                # 25-point locals within 50pts of spot (e.g. 6825, 6850)
                for offset in [-50, -25, 25, 50]:
                    lvl = round((spot_pm + offset) / 25) * 25
                    name = f"R{lvl:.0f}"
                    # Don't duplicate 100-pt rounds
                    if lvl % 100 != 0 and name not in key_levels:
                        key_levels[name] = float(lvl)
                # PM high/low from bars before 9:30
                pm_bars = [b for b in bars_pm if b.get("t") and
                           datetime.datetime.fromtimestamp(b["t"]/1000, ET).time() < datetime.time(9, 30)]
                if pm_bars:
                    key_levels["PM High"] = round(max(b["h"] for b in pm_bars), 2)
                    key_levels["PM Low"]  = round(min(b["l"] for b in pm_bars), 2)
            send_telegram(format_premarket_message(vix, key_levels, today_events))
            print(f"[TELEGRAM SENT] Pre-market brief")
            premarket_sent = True

        # RTH loop
        if is_market_open():
            in_blackout, event_name, mins_away, when = check_event_blackout(today_events)
            if in_blackout:
                print(f"[{now_et.strftime('%H:%M ET')}] BLACKOUT — {event_name} ({mins_away}min {when})")
                time.sleep(POLL_INTERVAL_SEC)
                continue

            if alert_count >= MAX_ALERTS_PER_DAY:
                print(f"[{now_et.strftime('%H:%M ET')}] Max alerts reached.")
                time.sleep(POLL_INTERVAL_SEC)
                continue

            bars = get_spx_bars(limit=80)
            if not bars or len(bars) < 10:
                print(f"[{now_et.strftime('%H:%M ET')}] Insufficient bars ({len(bars) if bars else 0}).")
                time.sleep(POLL_INTERVAL_SEC)
                continue

            spot     = bars[-1]["c"]
            vix      = get_vix()
            vwap     = calc_vwap(bars)
            momentum = calc_momentum(bars, 3)
            session  = get_session(now_et)
            vix_str  = f"{vix:.1f}" if vix else "N/A"

            print(f"[{now_et.strftime('%H:%M ET')}] SPX={spot:,.2f} VWAP={vwap} VIX={vix_str} Mom={momentum:+.1f} Alerts={alert_count}/{MAX_ALERTS_PER_DAY}")

            # VIX to key_levels for display only
            if vwap:
                key_levels["VWAP"] = vwap

            # Opening range (9:30–9:45)
            or_end = now_et.replace(hour=9, minute=45, second=0, microsecond=0)
            or_start = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
            if now_et < or_end:
                rth_bars = [b for b in bars if b.get("t") and
                            datetime.datetime.fromtimestamp(b["t"]/1000, ET) >= or_start]
                if rth_bars:
                    or_high = max(b["h"] for b in rth_bars)
                    or_low  = min(b["l"] for b in rth_bars)
                print(f"  → Building OR: {or_low} - {or_high}")
                time.sleep(POLL_INTERVAL_SEC)
                continue
            elif not or_set and or_high and or_low:
                or_set = True
                key_levels["OR High"] = round(or_high, 2)
                key_levels["OR Low"]  = round(or_low, 2)
                print(f"  → OR locked: {or_low:.2f} - {or_high:.2f}")
                send_telegram(
                    f"📊 *Opening Range Set*\nHigh: {or_high:,.2f}\nLow: {or_low:,.2f}\nScanning for breakouts..."
                )

            # VIX spike alert
            if vix and last_vix:
                vix_chg = (vix - last_vix) / last_vix * 100
                if vix_chg >= 8 and last_vix_spike != round(vix, 1):
                    last_vix_spike = round(vix, 1)
                    send_telegram(
                        f"⚠️ *VIX SPIKE*\nVIX {last_vix:.1f} → {vix:.1f} ({vix_chg:+.1f}%)\n"
                        f"SPX={spot:,.2f}\nWait for direction before entry"
                    )
            if vix:
                last_vix = vix

            # Hourly heartbeat
            if last_heartbeat is None or (now_et - last_heartbeat).total_seconds() >= 3600:
                last_heartbeat = now_et
                send_telegram(
                    f"💓 *Bot Heartbeat* — {now_et.strftime('%I:%M %p ET')}\n"
                    f"SPX={spot:,.2f} | VWAP={vwap} | VIX={vix_str}\n"
                    f"Alerts today: {alert_count}/{MAX_ALERTS_PER_DAY}"
                )

            # Telegram thread health
            global _telegram_thread
            if not _telegram_thread.is_alive():
                print("[WARN] Telegram thread dead — restarting")
                _telegram_thread = threading.Thread(target=_telegram_worker, daemon=True)
                _telegram_thread.start()

            # Only signal after OR is set and in entry window
            if or_set and now_et.time() <= datetime.time(15, 30):
                sig = evaluate_breakout(
                    bars, key_levels, vwap, vix, session,
                    last_signal_time, last_signal_price, last_signal_bias
                )
                if sig:
                    alert_count      += 1
                    last_signal_time  = now_et
                    last_signal_price = sig["spot"]
                    last_signal_bias  = sig["bias"]
                    msg = format_signal_message(sig, alert_count, MAX_ALERTS_PER_DAY)
                    send_telegram(msg)
                    log_signal(sig)
                    print(f"  → SIGNAL: {sig['trigger']} | {sig['bias']} | {sig['quality']} score={sig['score']}")
                else:
                    print(f"  → No breakout signal")
            elif not or_set:
                print(f"  → Waiting for OR to be set")

        else:
            print(f"[{now_et.strftime('%H:%M ET')}] Market closed.")

        time.sleep(POLL_INTERVAL_SEC)

# ─────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────
app = Flask(__name__)

@app.route("/health")
def health():
    return {"status": "ok", "bot": "SPX 0DTE Breakout Bot v4"}, 200

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
            print(f"[CRITICAL] Crashed: {e} — restarting in 30s")
            send_telegram(f"⚠️ *Bot Crashed*\n{str(e)[:100]}\nRestarting in 30s...")
            time.sleep(30)
