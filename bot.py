#!/usr/bin/env python3
"""
bot.py

SPX intraday signal bot with:
- Polygon primary data source
- yfinance fallback
- RTH VWAP
- Opening range logic
- Compression -> expansion detection
- Early Trend Continuation Mode
- Telegram alerts

Environment variables:
POLYGON_API_KEY
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID

Optional:
BOT_POLL_SECONDS=60
BOT_TIMEZONE=America/New_York
"""

import os
import time
import math
import json
import requests
import traceback
from dataclasses import dataclass
from datetime import datetime, date, time as dtime, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional, Tuple


# =========================
# CONFIG
# =========================

TZ = ZoneInfo(os.getenv("BOT_TIMEZONE", "America/New_York"))

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

POLL_SECONDS = int(os.getenv("BOT_POLL_SECONDS", "60"))

RTH_OPEN = dtime(9, 30)
RTH_CLOSE = dtime(16, 0)
SCAN_START = dtime(9, 30)
SCAN_END = dtime(15, 30)

SPX_TICKER_POLYGON = "I:SPX"
VIX_TICKER_POLYGON = "I:VIX"

# Normal signal engine
MOMENTUM_LOOKBACK_BARS = 3
NORMAL_MOMENTUM_MIN = 6.0
BAR_CONFIRMATION_COUNT = 2
VWAP_MAX_DISTANCE_NORMAL = 35.0
SIGNAL_COOLDOWN_MINUTES = 15
MAX_SIGNALS_PER_DAY = 5

# Opening range
OPENING_RANGE_MINUTES = 15

# Compression detection
COMPRESSION_ENABLED = True
COMPRESSION_RECENT_BARS = 5
COMPRESSION_BASELINE_BARS = 15
COMPRESSION_RANGE_RATIO = 0.60
COMPRESSION_ATR_MULT = 0.50
COMPRESSION_LEVEL_PROXIMITY = 8.0
COMPRESSION_EXPANSION_MULT = 1.15
COMPRESSION_SCORE_BONUS = 2.0

# Early Trend Continuation Mode
EARLY_TREND_ENABLED = True
EARLY_TREND_MIN_BARS_AFTER_OPEN = 20
EARLY_TREND_MAX_DISTANCE_FROM_VWAP = 18.0
EARLY_TREND_MIN_MOMENTUM = 2.5
EARLY_TREND_VWAP_SLOPE_LOOKBACK = 5
EARLY_TREND_PULLBACK_LOOKBACK = 8
EARLY_TREND_COOLDOWN_MINUTES = 20
EARLY_TREND_MAX_SIGNALS_PER_DAY = 2

# VIX regimes
VIX_ELEVATED = 20.0
VIX_HIGH_VOL = 30.0

# Daily GT/manual levels file. Optional.
# Expected JSON example:
# {
#   "date": "2026-05-01",
#   "pivot": 6852,
#   "r1": 6905,
#   "r2": 6977,
#   "monthly_1sd_upper": 7300,
#   "prior_day_high": 7212.6,
#   "prior_day_low": 7129.0,
#   "prior_day_close": 7207.1
# }
LEVELS_FILE = os.getenv("LEVELS_FILE", "levels.json")


# =========================
# DATA STRUCTURES
# =========================

@dataclass
class BotState:
    current_day: Optional[date] = None
    last_signal_time: Optional[datetime] = None
    signals_today: int = 0
    last_early_trend_signal_time: Optional[datetime] = None
    early_trend_signals_today: int = 0
    alerted_signal_keys: set = None

    def __post_init__(self):
        if self.alerted_signal_keys is None:
            self.alerted_signal_keys = set()

    def reset_if_new_day(self, now: datetime):
        if self.current_day != now.date():
            self.current_day = now.date()
            self.last_signal_time = None
            self.signals_today = 0
            self.last_early_trend_signal_time = None
            self.early_trend_signals_today = 0
            self.alerted_signal_keys = set()


STATE = BotState()


# =========================
# UTILS
# =========================

def now_et() -> datetime:
    return datetime.now(TZ)


def in_rth(now: datetime) -> bool:
    return RTH_OPEN <= now.time() <= RTH_CLOSE and now.weekday() < 5


def in_scan_window(now: datetime) -> bool:
    return SCAN_START <= now.time() <= SCAN_END and now.weekday() < 5


def minutes_since_open(now: datetime) -> int:
    open_dt = datetime.combine(now.date(), RTH_OPEN, tzinfo=TZ)
    return max(0, int((now - open_dt).total_seconds() // 60))


def safe_float(x, default=None):
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def fmt(x, digits=1):
    if x is None:
        return "n/a"
    try:
        return f"{float(x):.{digits}f}"
    except Exception:
        return "n/a"


# =========================
# TELEGRAM
# =========================

def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TELEGRAM DISABLED]", message)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code >= 300:
            print("Telegram error:", r.status_code, r.text)
    except Exception as e:
        print("Telegram exception:", e)


# =========================
# DATA FETCHING
# =========================

def polygon_get(url: str, params: Dict = None) -> Optional[dict]:
    if not POLYGON_API_KEY:
        return None

    params = params or {}
    params["apiKey"] = POLYGON_API_KEY

    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            print(f"Polygon error {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print("Polygon request exception:", e)
        time.sleep(1 + attempt)
    return None


def fetch_polygon_1m_bars(ticker: str, day: date) -> List[Dict]:
    day_str = day.isoformat()
    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/minute/{day_str}/{day_str}"
    data = polygon_get(url, {"adjusted": "true", "sort": "asc", "limit": 5000})
    if not data or "results" not in data:
        return []

    bars = []
    for x in data["results"]:
        ts = datetime.fromtimestamp(x["t"] / 1000, tz=ZoneInfo("UTC")).astimezone(TZ)
        if RTH_OPEN <= ts.time() <= RTH_CLOSE:
            bars.append({
                "time": ts,
                "open": float(x["o"]),
                "high": float(x["h"]),
                "low": float(x["l"]),
                "close": float(x["c"]),
                "volume": float(x.get("v", 0) or 0),
            })
    return bars


def fetch_yfinance_1m_bars() -> List[Dict]:
    try:
        import yfinance as yf
        df = yf.download("^GSPC", period="1d", interval="1m", progress=False, auto_adjust=False)
        if df is None or df.empty:
            return []
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df.index = df.index.tz_convert(TZ)

        bars = []
        for ts, row in df.iterrows():
            if RTH_OPEN <= ts.time() <= RTH_CLOSE:
                bars.append({
                    "time": ts.to_pydatetime(),
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": float(row.get("Volume", 0) or 0),
                })
        return bars
    except Exception as e:
        print("yfinance fallback failed:", e)
        return []


def fetch_spx_bars(day: date) -> List[Dict]:
    bars = fetch_polygon_1m_bars(SPX_TICKER_POLYGON, day)
    if bars:
        return bars
    return fetch_yfinance_1m_bars()


def fetch_vix() -> Optional[float]:
    bars = fetch_polygon_1m_bars(VIX_TICKER_POLYGON, now_et().date())
    if bars:
        return bars[-1]["close"]

    try:
        import yfinance as yf
        df = yf.download("^VIX", period="1d", interval="1m", progress=False, auto_adjust=False)
        if df is not None and not df.empty:
            return float(df["Close"].dropna().iloc[-1])
    except Exception:
        pass

    return None


# =========================
# INDICATORS
# =========================

def add_rth_vwap(bars: List[Dict]) -> List[Dict]:
    cumulative_pv = 0.0
    cumulative_v = 0.0

    for b in bars:
        typical = (b["high"] + b["low"] + b["close"]) / 3.0
        vol = b.get("volume", 0) or 1.0

        # Index bars sometimes have unreliable volume. Use 1 if volume is missing.
        if vol <= 0:
            vol = 1.0

        cumulative_pv += typical * vol
        cumulative_v += vol
        b["vwap"] = cumulative_pv / cumulative_v if cumulative_v else None

    return bars


def atr(bars: List[Dict], lookback: int = 14) -> Optional[float]:
    if len(bars) < lookback + 1:
        return None

    trs = []
    recent = bars[-lookback:]
    prev_close = bars[-lookback - 1]["close"]

    for b in recent:
        tr = max(
            b["high"] - b["low"],
            abs(b["high"] - prev_close),
            abs(b["low"] - prev_close),
        )
        trs.append(tr)
        prev_close = b["close"]

    return sum(trs) / len(trs) if trs else None


def get_opening_range(bars: List[Dict]) -> Tuple[Optional[float], Optional[float]]:
    if not bars:
        return None, None

    open_dt = datetime.combine(bars[-1]["time"].date(), RTH_OPEN, tzinfo=TZ)
    cutoff = open_dt + timedelta(minutes=OPENING_RANGE_MINUTES)

    or_bars = [b for b in bars if open_dt <= b["time"] < cutoff]
    if len(or_bars) < OPENING_RANGE_MINUTES:
        return None, None

    return max(b["high"] for b in or_bars), min(b["low"] for b in or_bars)


def load_levels() -> Dict:
    try:
        p = Path(LEVELS_FILE)
        if p.exists():
            return json.loads(p.read_text())
    except Exception as e:
        print("Could not load levels file:", e)
    return {}


def get_prior_day_levels(levels: Dict, bars: List[Dict]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    pdh = safe_float(levels.get("prior_day_high"))
    pdl = safe_float(levels.get("prior_day_low"))
    pdc = safe_float(levels.get("prior_day_close"))
    return pdh, pdl, pdc


def classify_vix_regime(vix: Optional[float]) -> str:
    if vix is None:
        return "UNKNOWN"
    if vix >= VIX_HIGH_VOL:
        return "HIGH_VOL"
    if vix >= VIX_ELEVATED:
        return "ELEVATED"
    return "NORMAL"


# =========================
# SIGNAL HELPERS
# =========================

def recent_momentum(bars: List[Dict], lookback: int = MOMENTUM_LOOKBACK_BARS) -> Optional[float]:
    if len(bars) < lookback + 1:
        return None
    return bars[-1]["close"] - bars[-1 - lookback]["close"]


def consecutive_green_bars(bars: List[Dict], n: int) -> bool:
    if len(bars) < n:
        return False
    recent = bars[-n:]
    return all(b["close"] > b["open"] for b in recent)


def consecutive_red_bars(bars: List[Dict], n: int) -> bool:
    if len(bars) < n:
        return False
    recent = bars[-n:]
    return all(b["close"] < b["open"] for b in recent)


def nearest_level_distance(spot: float, levels: Dict) -> Tuple[Optional[str], Optional[float]]:
    candidates = {}
    for k, v in levels.items():
        fv = safe_float(v)
        if fv is not None:
            candidates[k] = fv

    if not candidates:
        return None, None

    name, val = min(candidates.items(), key=lambda kv: abs(spot - kv[1]))
    return name, abs(spot - val)


def detect_compression(bars: List[Dict], spot: float, levels: Dict) -> Tuple[bool, str, float]:
    if not COMPRESSION_ENABLED:
        return False, "compression disabled", 0.0

    needed = COMPRESSION_RECENT_BARS + COMPRESSION_BASELINE_BARS
    if len(bars) < needed + 1:
        return False, "not enough bars for compression", 0.0

    recent = bars[-COMPRESSION_RECENT_BARS:]
    baseline = bars[-needed:-COMPRESSION_RECENT_BARS]

    recent_avg_range = sum(b["high"] - b["low"] for b in recent) / len(recent)
    baseline_avg_range = sum(b["high"] - b["low"] for b in baseline) / len(baseline)

    a = atr(bars, 14)
    latest_range = bars[-1]["high"] - bars[-1]["low"]

    range_contraction = baseline_avg_range > 0 and recent_avg_range < baseline_avg_range * COMPRESSION_RANGE_RATIO
    atr_contraction = a is not None and latest_range < a * COMPRESSION_ATR_MULT

    level_name, level_dist = nearest_level_distance(spot, levels)
    near_level = level_dist is not None and level_dist <= COMPRESSION_LEVEL_PROXIMITY

    expansion_starting = baseline_avg_range > 0 and latest_range > recent_avg_range * COMPRESSION_EXPANSION_MULT

    score = 0.0
    if range_contraction:
        score += 0.75
    if atr_contraction:
        score += 0.50
    if near_level:
        score += 0.50
    if expansion_starting:
        score += 0.75

    ok = range_contraction and (near_level or expansion_starting)

    reason = (
        f"compression range={recent_avg_range:.1f} vs baseline={baseline_avg_range:.1f}, "
        f"latest={latest_range:.1f}, atr={fmt(a)}, near={level_name}:{fmt(level_dist)}"
    )

    return ok, reason, score


def detect_early_trend_continuation(
    bars: List[Dict],
    spot: Optional[float],
    vwap: Optional[float],
    prior_day_high: Optional[float] = None,
    opening_range_high: Optional[float] = None,
    vix_regime: str = "UNKNOWN",
) -> Tuple[bool, str]:
    """
    Early long continuation setup for trend/grind days.
    This fires before full momentum confirmation.
    Long-only.
    """

    if not EARLY_TREND_ENABLED:
        return False, "disabled"

    if vix_regime == "HIGH_VOL":
        return False, "high vol regime"

    if len(bars) < max(
        EARLY_TREND_MIN_BARS_AFTER_OPEN,
        EARLY_TREND_PULLBACK_LOOKBACK + 2,
        EARLY_TREND_VWAP_SLOPE_LOOKBACK + 2,
    ):
        return False, "not enough bars"

    if vwap is None or spot is None:
        return False, "missing spot/vwap"

    if spot <= vwap:
        return False, "spot below vwap"

    distance_from_vwap = spot - vwap
    if distance_from_vwap > EARLY_TREND_MAX_DISTANCE_FROM_VWAP:
        return False, f"too extended from vwap: {distance_from_vwap:.1f}"

    above_prior_high = prior_day_high is not None and spot > prior_day_high
    above_or_high = opening_range_high is not None and spot > opening_range_high

    if not (above_prior_high or above_or_high):
        return False, "not above prior high or opening range high"

    recent_vwaps = [
        b.get("vwap")
        for b in bars[-EARLY_TREND_VWAP_SLOPE_LOOKBACK:]
        if b.get("vwap") is not None
    ]

    if len(recent_vwaps) < EARLY_TREND_VWAP_SLOPE_LOOKBACK:
        return False, "insufficient vwap history"

    if recent_vwaps[-1] <= recent_vwaps[0]:
        return False, "vwap slope not positive"

    recent = bars[-EARLY_TREND_PULLBACK_LOOKBACK:]
    closes_above_vwap = 0

    for b in recent:
        c = b.get("close")
        vw = b.get("vwap")
        if c is not None and vw is not None and c >= vw:
            closes_above_vwap += 1

    if closes_above_vwap < max(5, EARLY_TREND_PULLBACK_LOOKBACK - 2):
        return False, "pullback did not hold vwap"

    last_three = bars[-3:]
    for b in last_three:
        high = b.get("high")
        close = b.get("close")
        vw = b.get("vwap")
        if high is not None and close is not None and vw is not None:
            if high > vw and close < vw:
                return False, "recent vwap rejection"

    momentum = recent_momentum(bars, 3)
    if momentum is None:
        return False, "missing momentum"

    if momentum < EARLY_TREND_MIN_MOMENTUM:
        return False, f"momentum too weak: {momentum:.1f}"

    structure = []
    if above_prior_high:
        structure.append("above prior high")
    if above_or_high:
        structure.append("above opening range high")

    reason = (
        f"EARLY TREND CONTINUATION: spot {spot:.1f} > VWAP {vwap:.1f}, "
        f"distance {distance_from_vwap:.1f}, VWAP rising, "
        f"{' and '.join(structure)}, light momentum {momentum:.1f}"
    )

    return True, reason


def in_cooldown(last_time: Optional[datetime], minutes: int, now: datetime) -> bool:
    if last_time is None:
        return False
    return (now - last_time).total_seconds() < minutes * 60


def build_alert(
    title: str,
    signal_type: str,
    spot: float,
    vwap: Optional[float],
    momentum: Optional[float],
    vix: Optional[float],
    vix_regime: str,
    prior_day_high: Optional[float],
    opening_range_high: Optional[float],
    reason: str,
    levels: Dict,
) -> str:
    distance = spot - vwap if vwap is not None else None

    lines = [
        f"<b>{title}</b>",
        "",
        f"Type: {signal_type}",
        f"Spot: {fmt(spot)}",
        f"VWAP: {fmt(vwap)}",
        f"Distance from VWAP: {fmt(distance)}",
        f"3-bar momentum: {fmt(momentum)}",
        f"VIX: {fmt(vix)} ({vix_regime})",
        f"Prior day high: {fmt(prior_day_high)}",
        f"Opening range high: {fmt(opening_range_high)}",
    ]

    for key in ["pivot", "r1", "r2", "monthly_1sd_upper", "daily_1sd_upper", "weekly_1sd_upper"]:
        if key in levels:
            lines.append(f"{key}: {fmt(levels.get(key))}")

    lines += [
        "",
        reason,
    ]

    return "\n".join(lines)


# =========================
# MAIN SIGNAL ENGINE
# =========================

def evaluate_signals(bars: List[Dict], levels: Dict, vix: Optional[float]):
    now = now_et()
    STATE.reset_if_new_day(now)

    if not bars:
        print("No bars available")
        return

    if not in_scan_window(now):
        print(f"{now.strftime('%H:%M:%S')} Market outside scan window")
        return

    bars = add_rth_vwap(bars)
    spot = bars[-1]["close"]
    current_vwap = bars[-1].get("vwap")
    momentum = recent_momentum(bars, 3)
    vix_regime = classify_vix_regime(vix)

    prior_day_high, prior_day_low, prior_day_close = get_prior_day_levels(levels, bars)
    opening_range_high, opening_range_low = get_opening_range(bars)

    mins_open = minutes_since_open(now)

    print(
        f"{now.strftime('%H:%M:%S')} spot={fmt(spot)} vwap={fmt(current_vwap)} "
        f"mom3={fmt(momentum)} vix={fmt(vix)} regime={vix_regime}"
    )

    # -------------------------
    # EARLY TREND MODE
    # -------------------------
    if mins_open >= EARLY_TREND_MIN_BARS_AFTER_OPEN:
        early_ok, early_reason = detect_early_trend_continuation(
            bars=bars,
            spot=spot,
            vwap=current_vwap,
            prior_day_high=prior_day_high,
            opening_range_high=opening_range_high,
            vix_regime=vix_regime,
        )

        if early_ok:
            if STATE.early_trend_signals_today < EARLY_TREND_MAX_SIGNALS_PER_DAY:
                if not in_cooldown(STATE.last_early_trend_signal_time, EARLY_TREND_COOLDOWN_MINUTES, now):
                    signal_key = f"early_trend_{now.date()}_{int(spot // 5) * 5}"
                    if signal_key not in STATE.alerted_signal_keys:
                        msg = build_alert(
                            title="EARLY TREND CONTINUATION LONG",
                            signal_type="EARLY_TREND_CONTINUATION",
                            spot=spot,
                            vwap=current_vwap,
                            momentum=momentum,
                            vix=vix,
                            vix_regime=vix_regime,
                            prior_day_high=prior_day_high,
                            opening_range_high=opening_range_high,
                            reason=early_reason,
                            levels=levels,
                        )
                        send_telegram(msg)
                        STATE.early_trend_signals_today += 1
                        STATE.last_early_trend_signal_time = now
                        STATE.alerted_signal_keys.add(signal_key)
                        return

    # -------------------------
    # NORMAL MOMENTUM ENGINE
    # -------------------------
    if STATE.signals_today >= MAX_SIGNALS_PER_DAY:
        return

    if in_cooldown(STATE.last_signal_time, SIGNAL_COOLDOWN_MINUTES, now):
        return

    if current_vwap is None or momentum is None:
        return

    distance = spot - current_vwap
    if abs(distance) > VWAP_MAX_DISTANCE_NORMAL:
        return

    compression_ok, compression_reason, compression_score = detect_compression(bars, spot, levels)

    long_structure = spot > current_vwap
    if opening_range_high is not None:
        long_structure = long_structure and spot > opening_range_high

    short_structure = spot < current_vwap
    if opening_range_low is not None:
        short_structure = short_structure and spot < opening_range_low

    long_score = 0.0
    short_score = 0.0

    if momentum >= NORMAL_MOMENTUM_MIN:
        long_score += 2.0
    if consecutive_green_bars(bars, BAR_CONFIRMATION_COUNT):
        long_score += 1.0
    if long_structure:
        long_score += 1.0
    if compression_ok and momentum > 0:
        long_score += COMPRESSION_SCORE_BONUS + compression_score

    if momentum <= -NORMAL_MOMENTUM_MIN:
        short_score += 2.0
    if consecutive_red_bars(bars, BAR_CONFIRMATION_COUNT):
        short_score += 1.0
    if short_structure:
        short_score += 1.0
    if compression_ok and momentum < 0:
        short_score += COMPRESSION_SCORE_BONUS + compression_score

    # Avoid bearish countertrend above VWAP on obvious long structure days
    if spot > current_vwap and short_score > 0:
        short_score = max(0.0, short_score - 2.0)

    if long_score >= 3.5:
        signal_key = f"normal_long_{now.date()}_{int(spot // 5) * 5}"
        if signal_key not in STATE.alerted_signal_keys:
            reason = (
                f"Momentum confirmation long: score {long_score:.1f}, "
                f"momentum {momentum:.1f}, spot above VWAP. {compression_reason if compression_ok else ''}"
            )
            msg = build_alert(
                title="SPX MOMENTUM LONG",
                signal_type="MOMENTUM_LONG",
                spot=spot,
                vwap=current_vwap,
                momentum=momentum,
                vix=vix,
                vix_regime=vix_regime,
                prior_day_high=prior_day_high,
                opening_range_high=opening_range_high,
                reason=reason,
                levels=levels,
            )
            send_telegram(msg)
            STATE.signals_today += 1
            STATE.last_signal_time = now
            STATE.alerted_signal_keys.add(signal_key)
            return

    if short_score >= 4.0 and vix_regime != "HIGH_VOL":
        signal_key = f"normal_short_{now.date()}_{int(spot // 5) * 5}"
        if signal_key not in STATE.alerted_signal_keys:
            reason = (
                f"Momentum confirmation short: score {short_score:.1f}, "
                f"momentum {momentum:.1f}, spot below VWAP. {compression_reason if compression_ok else ''}"
            )
            msg = build_alert(
                title="SPX MOMENTUM SHORT",
                signal_type="MOMENTUM_SHORT",
                spot=spot,
                vwap=current_vwap,
                momentum=momentum,
                vix=vix,
                vix_regime=vix_regime,
                prior_day_high=prior_day_high,
                opening_range_high=opening_range_high,
                reason=reason,
                levels=levels,
            )
            send_telegram(msg)
            STATE.signals_today += 1
            STATE.last_signal_time = now
            STATE.alerted_signal_keys.add(signal_key)
            return


# =========================
# STATUS / MAIN LOOP
# =========================

def status_message(bars: List[Dict], levels: Dict, vix: Optional[float]) -> str:
    if bars:
        bars = add_rth_vwap(bars)
        spot = bars[-1]["close"]
        vwap = bars[-1].get("vwap")
        mom = recent_momentum(bars, 3)
    else:
        spot = vwap = mom = None

    orh, orl = get_opening_range(bars) if bars else (None, None)
    pdh = safe_float(levels.get("prior_day_high"))

    return (
        f"STATUS {now_et().strftime('%Y-%m-%d %H:%M:%S ET')}\n"
        f"Spot: {fmt(spot)}\n"
        f"VWAP: {fmt(vwap)}\n"
        f"3-bar momentum: {fmt(mom)}\n"
        f"VIX: {fmt(vix)} ({classify_vix_regime(vix)})\n"
        f"Prior high: {fmt(pdh)}\n"
        f"OR high: {fmt(orh)}\n"
        f"Early trend signals today: {STATE.early_trend_signals_today}\n"
        f"Normal signals today: {STATE.signals_today}"
    )


def run_once():
    now = now_et()
    STATE.reset_if_new_day(now)

    levels = load_levels()
    bars = fetch_spx_bars(now.date())
    vix = fetch_vix()

    if not in_rth(now):
        print(f"{now.strftime('%Y-%m-%d %H:%M:%S')} Market closed")
        return

    evaluate_signals(bars, levels, vix)


def main():
    print("Starting SPX bot.py")
    print(f"Timezone: {TZ}")
    print(f"Polygon enabled: {bool(POLYGON_API_KEY)}")
    print(f"Telegram enabled: {bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)}")

    send_telegram("SPX bot started. Early Trend Continuation Mode enabled.")

    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            print("Stopped by user")
            break
        except Exception:
            print("Unhandled bot error:")
            traceback.print_exc()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
