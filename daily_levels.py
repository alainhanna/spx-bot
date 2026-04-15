# ============================================================
# SPX DAILY LEVELS — Generated from John's charts
# Date: April 15, 2026
# Current Price at capture: ~6,969
#
# IMPORTANT: This file is CONTEXT ONLY.
# It does not gate, suppress, or override signals.
# Bias is always calculated dynamically from live price vs pivot.
# Levels influence confidence commentary only.
# ============================================================

DAILY_LEVELS = {

    # ── BULL/BEAR PIVOT ─────────────────────────────────────
    # bias = "long" if price > bull_bear_pivot else "short"
    "bull_bear_pivot": 6960,

    # ── GAMMA FLIP ──────────────────────────────────────────
    "gamma_flip": 6763,

    # ── STANDARD DEVIATION BANDS ────────────────────────────
    "daily_1sd_upper": 7011,
    "daily_1sd_lower": 6924,
    "daily_2sd_upper": 7054,
    "daily_2sd_lower": 6879,
    "weekly_1sd_upper": 6940,
    "weekly_1sd_lower": 6693,
    "weekly_2sd_upper": 7063,
    "weekly_2sd_lower": 6250,
    "monthly_1sd_upper": 6905,
    "monthly_2sd_upper": 7300,

    # ── YEARLY LEVELS ───────────────────────────────────────
    "yearly_1sd_upper": 8007,
    "yearly_2sd_upper": 9169,
    "yearly_1sd_lower": 5684,
    "yearly_2sd_lower": 4522,

    # ── QUARTERLY LEVELS ────────────────────────────────────
    "q2_upper": 7195,
    "q2_lower": 5861,

    # ── PIVOT POINTS (Classic R/S) ──────────────────────────
    "R3": 7002,
    "R2": 6986,
    "R1": 6977,
    "S1": 6950,
    "S2": 6934,
    "S3": 6925,

    # ── MOVING AVERAGES ─────────────────────────────────────
    "dma": {
        5:   6855,
        20:  6621,
        50:  6754,
        100: 6809,
        200: 6673,
    },

    # ── REFERENCE / CLOSING PRICES ──────────────────────────
    "close_2025": 6845,

    # ── VWAP LEVELS (Chart 2 — SPX VWAPs) ──────────────────
    "vwap_daily":  6952.85,
    "vwap_wtd":    6901.82,
    "vwap_level3": 6876.42,
    "vwap_level4": 6858.90,

    # ── KEY PRICE LADDER (Chart 2 — SPX VWAPs) ──────────────
    "key_price_ladder": {
        "nov_low":        6812.29,
        "ytd":            6808.31,
        "ath":            6773.37,
        "aug_low":        6729.19,
        "mtd":            6728.92,
        "ytd_low":        6679.18,
        "may_gap_up":     6584.31,
        "april_2025_low": 6468.57,
        "vwap_2025":      6335.99,
    },

    # ── NEAREST RESISTANCE to ~6969 ─────────────────────────
    "nearest_resistance": [
        {"level": 6977,  "label": "R1"},
        {"level": 6986,  "label": "R2"},
        {"level": 7002,  "label": "R3"},
        {"level": 7011,  "label": "Daily 1SD Upper"},
        {"level": 7054,  "label": "Daily 2SD Upper"},
        {"level": 7063,  "label": "Weekly 2SD Upper"},
        {"level": 7195,  "label": "Q2 Upper"},
        {"level": 7300,  "label": "Monthly 2SD Upper"},
    ],

    # ── NEAREST SUPPORT to ~6969 ────────────────────────────
    "nearest_support": [
        {"level": 6960,  "label": "Bull/Bear Pivot"},
        {"level": 6952,  "label": "Daily VWAP"},
        {"level": 6940,  "label": "Weekly 1SD Upper"},
        {"level": 6934,  "label": "S2"},
        {"level": 6925,  "label": "S3"},
        {"level": 6924,  "label": "Daily 1SD Lower"},
        {"level": 6905,  "label": "Monthly 1SD Upper"},
        {"level": 6901,  "label": "WTD VWAP"},
        {"level": 6879,  "label": "Daily 2SD Lower"},
        {"level": 6855,  "label": "5 DMA"},
        {"level": 6845,  "label": "2025 Closing Price"},
        {"level": 6812,  "label": "Nov Low"},
        {"level": 6809,  "label": "100 DMA"},
        {"level": 6808,  "label": "YTD"},
        {"level": 6773,  "label": "ATH"},
        {"level": 6763,  "label": "Gamma Flip"},
        {"level": 6754,  "label": "50 DMA"},
        {"level": 6729,  "label": "Aug Low"},
        {"level": 6693,  "label": "Weekly 1SD Lower"},
        {"level": 6679,  "label": "YTD Low"},
        {"level": 6673,  "label": "200 DMA"},
        {"level": 6621,  "label": "20 DMA"},
    ],

    # ── SCENARIO MAP ────────────────────────────────────────
    "scenarios": {
        "bull": {
            "condition": "Holds above Bull/Bear Pivot 6960",
            "targets": [6977, 6986, 7002, 7011, 7054],
            "description": "R1 → R2 → R3 → Daily 1SD Upper → Daily 2SD Upper"
        },
        "bear": {
            "condition": "Loses Bull/Bear Pivot 6960",
            "targets": [6952, 6940, 6924, 6905, 6879],
            "description": "Daily VWAP → Weekly 1SD Upper → Daily 1SD Lower → Monthly 1SD → Daily 2SD Lower"
        },
        "chop": {
            "range_low":  6924,
            "range_high": 7011,
            "description": "Inside daily 1SD band — no directional edge"
        }
    }
}


# ── HELPER: dynamic bias ─────────────────────────────────────
def get_bias(price):
    """Dynamically calculate bias from live price vs pivot. Never use static."""
    return "long" if price > DAILY_LEVELS["bull_bear_pivot"] else "short"


# ── HELPER: distance to a single level ──────────────────────
def distance_to_level(price, level):
    """Return absolute point distance between price and a level."""
    return round(abs(price - level), 2)


# ── HELPER: nearest levels lookup ───────────────────────────
def get_nearest_levels(price, n=3):
    """Return the n closest resistance and support levels to current price."""
    res = [(l["level"], l["label"]) for l in DAILY_LEVELS["nearest_resistance"] if l["level"] > price]
    sup = [(l["level"], l["label"]) for l in DAILY_LEVELS["nearest_support"]   if l["level"] < price]
    res_sorted = sorted(res, key=lambda x: x[0])[:n]
    sup_sorted = sorted(sup, key=lambda x: x[0], reverse=True)[:n]
    return {"resistance": res_sorted, "support": sup_sorted}


# ── HELPER: confidence modifier ─────────────────────────────
def get_confidence_modifier(price):
    """
    Returns a confidence modifier and note for the signal engine.
    Signals always fire — this only adjusts confidence commentary.
    Returns WATCH near 1SD, CAUTION near 2SD, NORMAL otherwise.
    """
    d2u = DAILY_LEVELS["daily_2sd_upper"]
    d2l = DAILY_LEVELS["daily_2sd_lower"]
    d1u = DAILY_LEVELS["daily_1sd_upper"]
    d1l = DAILY_LEVELS["daily_1sd_lower"]

    if distance_to_level(price, d2u) <= 10 or distance_to_level(price, d2l) <= 10:
        return {"modifier": "CAUTION", "note": "Near 2SD extension — elevated reversal risk"}
    elif distance_to_level(price, d1u) <= 8:
        return {"modifier": "WATCH", "note": f"Approaching Daily 1SD Upper {d1u} — resistance ahead"}
    elif distance_to_level(price, d1l) <= 8:
        return {"modifier": "WATCH", "note": f"Approaching Daily 1SD Lower {d1l} — support nearby"}
    else:
        return {"modifier": "NORMAL", "note": ""}


# ── HELPER: full alert context string ───────────────────────
def get_alert_context(price):
    """
    Returns a formatted context string to append to Telegram signal alerts.
    """
    bias   = get_bias(price).upper()
    pivot  = DAILY_LEVELS["bull_bear_pivot"]
    levels = get_nearest_levels(price, n=1)
    conf   = get_confidence_modifier(price)

    r_entry = levels["resistance"][0] if levels["resistance"] else None
    s_entry = levels["support"][0]    if levels["support"]    else None

    r_level, r_label = (r_entry[0], r_entry[1]) if r_entry else (None, None)
    s_level, s_label = (s_entry[0], s_entry[1]) if s_entry else (None, None)

    r_str = f"{r_level} ({r_label}, {distance_to_level(price, r_level)}pts)" if r_level else "no resistance data"
    s_str = f"{s_level} ({s_label}, {distance_to_level(price, s_level)}pts)" if s_level else "no support data"

    context = f"Bias: {bias} | Pivot: {pivot} | R: {r_str} | S: {s_str}"
    if conf["note"]:
        context += f" | {conf['modifier']}: {conf['note']}"

    return context
