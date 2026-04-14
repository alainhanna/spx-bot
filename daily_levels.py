# ============================================================
# SPX DAILY LEVELS — Generated from John's charts
# Date: April 14, 2026
# Current Price at capture: ~6,886–6,889
#
# IMPORTANT: This file is CONTEXT ONLY.
# It does not gate, suppress, or override signals.
# Bias is always calculated dynamically from live price vs pivot.
# Levels influence confidence commentary only.
# ============================================================

DAILY_LEVELS = {

    # ── BULL/BEAR PIVOT ─────────────────────────────────────
    # DO NOT use static bias — always calculate dynamically:
    # bias = "long" if price > bull_bear_pivot else "short"
    "bull_bear_pivot": 6852,        # key line in sand

    # ── GAMMA FLIP ──────────────────────────────────────────
    "gamma_flip": 6751,             # dealer hedging flip point

    # ── STANDARD DEVIATION BANDS ────────────────────────────
    "daily_1sd_upper": 6927,
    "daily_1sd_lower": 6845,        # also = 2025 closing price
    "daily_2sd_upper": 6969,        # extreme — reduce confidence near here
    "daily_2sd_lower": 6804,        # extreme — reduce confidence near here
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
    "R3": 7008,
    "R2": 6945,
    "R1": 6915,
    "S1": 6823,
    "S2": 6759,
    "S3": 6729,

    # ── MOVING AVERAGES ─────────────────────────────────────
    "dma": {
        5:   6785,
        20:  6609,
        50:  6754,
        100: 6805,
        200: 6669,
    },

    # ── REFERENCE / CLOSING PRICES ──────────────────────────
    "close_2025": 6845,             # also = daily 1SD lower

    # ── KEY PRICE LADDER (Chart 2) ──────────────────────────
    "key_price_ladder": {
        "nov_low":        6822.80,
        "ytd":            6815.89,
        "ath":            6815.29,
        "aug_low":        6810.60,
        "mtd":            6797.44,
        "level_6769":     6769.16,
        "ytd_low":        6749.11,
        "level_6727":     6727.11,  # 12th May gap up
        "level_6680":     6680.15,
        "level_6629":     6629.42,
        "level_6581":     6581.77,
        "april_2025_low": 6465.45,
        "vwap_2025":      6332.75,
    },

    # ── NEAREST LEVELS TO CURRENT PRICE (~6889) ─────────────
    "nearest_resistance": [
        {"level": 6905,  "label": "Monthly 1SD Upper"},
        {"level": 6915,  "label": "R1"},
        {"level": 6927,  "label": "Daily 1SD Upper"},
        {"level": 6940,  "label": "Weekly 1SD Upper"},
        {"level": 6945,  "label": "R2"},
        {"level": 6969,  "label": "Daily 2SD Upper"},
        {"level": 7008,  "label": "R3"},
        {"level": 7063,  "label": "Weekly 2SD Upper"},
        {"level": 7195,  "label": "Q2 Upper"},
        {"level": 7300,  "label": "Monthly 2SD Upper"},
    ],
    "nearest_support": [
        {"level": 6852,  "label": "Bull/Bear Pivot"},
        {"level": 6845,  "label": "Daily 1SD Lower / 2025 Close"},
        {"level": 6823,  "label": "S1 / Nov Low"},
        {"level": 6815,  "label": "ATH / YTD"},
        {"level": 6805,  "label": "100 DMA"},
        {"level": 6804,  "label": "Daily 2SD Lower"},
        {"level": 6797,  "label": "MTD"},
        {"level": 6785,  "label": "5 DMA"},
        {"level": 6769,  "label": "Key Level"},
        {"level": 6759,  "label": "S2"},
        {"level": 6754,  "label": "50 DMA"},
        {"level": 6751,  "label": "Gamma Flip"},
        {"level": 6749,  "label": "YTD Low"},
        {"level": 6729,  "label": "S3"},
        {"level": 6693,  "label": "Weekly 1SD Lower"},
        {"level": 6669,  "label": "200 DMA"},
    ],

    # ── SCENARIO MAP ────────────────────────────────────────
    "scenarios": {
        "bull": {
            "condition": "Holds above 6852 pivot",
            "targets": [6905, 6915, 6927, 6945, 6969],
            "description": "Monthly 1SD → R1 → Daily 1SD Upper → R2 → Daily 2SD"
        },
        "bear": {
            "condition": "Loses 6852 pivot",
            "targets": [6845, 6823, 6805, 6785, 6754, 6751],
            "description": "2025 Close/1SD Lower → S1 → 100DMA → 5DMA → 50DMA → Gamma Flip"
        },
        "chop": {
            "range_low":  6845,
            "range_high": 6927,
            "description": "Inside daily 1SD band — no directional edge"
        }
    }
}


# ── HELPER: dynamic bias (always call this, never use static field) ──
def get_bias(price):
    """Dynamically calculate bias from live price vs pivot. Never use static."""
    pivot = DAILY_LEVELS["bull_bear_pivot"]
    return "long" if price > pivot else "short"


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
    Example output:
      Bias: LONG | Pivot: 6852 | R: 6905 (Monthly 1SD, 16pts) | S: 6852 (Pivot, 37pts) | WATCH: Approaching Daily 1SD Upper
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
