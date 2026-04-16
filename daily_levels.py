# ============================================================
# SPX DAILY LEVELS — Generated from John's charts
# Date: April 16, 2026
# Current Price at capture: ~7,038
#
# IMPORTANT: This file is CONTEXT ONLY.
# It does not gate, suppress, or override signals.
# Bias is always calculated dynamically from live price vs pivot.
# Levels influence confidence commentary only.
# ============================================================

DAILY_LEVELS = {

    # ── BULL/BEAR PIVOT ─────────────────────────────────────
    "bull_bear_pivot": 7010,

    # ── GAMMA FLIP ──────────────────────────────────────────
    "gamma_flip": 6763,

    # ── STANDARD DEVIATION BANDS ────────────────────────────
    "daily_1sd_upper": 7066,
    "daily_1sd_lower": 6975,
    "daily_2sd_upper": 7109,
    "daily_2sd_lower": 6935,
    "weekly_1sd_upper": 6940,
    "weekly_1sd_lower": 6693,  # scale-estimated
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
    "R3": 7081,
    "R2": 7053,
    "R1": 7038,
    "S1": 6995,
    "S2": 6967,
    "S3": 6952,

    # ── MOVING AVERAGES ─────────────────────────────────────
    "dma": {
        5:   6903,
        20:  6641,
        50:  6754,
        100: 6813,
        200: 6677,
    },

    # ── REFERENCE / CLOSING PRICES ──────────────────────────
    "close_2025": 6845,

    # ── GEX LEVELS (MenthorQ — Apr 16 expiration) ───────────
    "gex_call_resistance_0dte": 7060,
    "gex_put_support_0dte":     6840,
    "gex_hvl_0dte":             6855,
    "gex_call_resistance_1dte": 7000,
    "gex_put_support_1dte":     6950,

    # ── VWAP LEVELS (SPX VWAPs chart) ───────────────────────
    "vwap_daily":  6937.66,
    "vwap_wtd":    6910.58,
    "vwap_level3": 6979.38,
    "ath":         7022.96,

    # ── KEY PRICE LADDER (SPX VWAPs chart) ──────────────────
    "key_price_ladder": {
        "nov_low":        6815.38,
        "ytd":            6810.83,
        "mtd":            6758.33,
        "aug_low":        6731.54,
        "ytd_low":        6708.14,
        "may_gap_up":     6587.94,
        "april_2025_low": 6474.84,
        "vwap_2025":      6338.45,
    },

    # ── NEAREST RESISTANCE to ~7038 ─────────────────────────
    "nearest_resistance": [
        {"level": 7038,  "label": "R1"},
        {"level": 7053,  "label": "R2"},
        {"level": 7060,  "label": "GEX Call Resistance 0DTE"},
        {"level": 7066,  "label": "Daily 1SD Upper"},
        {"level": 7081,  "label": "R3"},
        {"level": 7109,  "label": "Daily 2SD Upper"},
        {"level": 7195,  "label": "Q2 Upper"},
        {"level": 7300,  "label": "Monthly 2SD Upper"},
    ],

    # ── NEAREST SUPPORT to ~7038 ────────────────────────────
    "nearest_support": [
        {"level": 7022,  "label": "ATH"},
        {"level": 7010,  "label": "Bull/Bear Pivot"},
        {"level": 6995,  "label": "S1"},
        {"level": 6979,  "label": "Daily VWAP L3"},
        {"level": 6975,  "label": "Daily 1SD Lower"},
        {"level": 6967,  "label": "S2"},
        {"level": 6952,  "label": "S3"},
        {"level": 6940,  "label": "Weekly 1SD Upper"},
        {"level": 6938,  "label": "Daily VWAP"},
        {"level": 6935,  "label": "Daily 2SD Lower"},
        {"level": 6911,  "label": "WTD VWAP"},
        {"level": 6903,  "label": "5 DMA"},
        {"level": 6855,  "label": "GEX HVL 0DTE"},
        {"level": 6845,  "label": "2025 Closing Price"},
        {"level": 6840,  "label": "GEX Put Support 0DTE"},
        {"level": 6813,  "label": "100 DMA"},
        {"level": 6763,  "label": "Gamma Flip"},
        {"level": 6754,  "label": "50 DMA"},
        {"level": 6677,  "label": "200 DMA"},
        {"level": 6693,  "label": "Weekly 1SD Lower"},
        {"level": 6641,  "label": "20 DMA"},
    ],

    # ── SCENARIO MAP ────────────────────────────────────────
    "scenarios": {
        "bull": {
            "condition": "Holds above Bull/Bear Pivot 7010",
            "targets": [7038, 7053, 7060, 7066, 7081, 7109],
            "description": "R1 -> R2 -> GEX Call Resistance -> Daily 1SD Upper -> R3 -> Daily 2SD Upper"
        },
        "bear": {
            "condition": "Loses Bull/Bear Pivot 7010",
            "targets": [6995, 6979, 6975, 6967, 6952, 6940],
            "description": "S1 -> VWAP L3 -> Daily 1SD Lower -> S2 -> S3 -> Weekly 1SD Upper"
        },
        "chop": {
            "range_low":  6975,
            "range_high": 7066,
            "description": "Inside daily 1SD band - no directional edge"
        }
    }
}


def get_bias(price):
    return "long" if price > DAILY_LEVELS["bull_bear_pivot"] else "short"


def distance_to_level(price, level):
    return round(abs(price - level), 2)


def get_nearest_levels(price, n=3):
    res = [(l["level"], l["label"]) for l in DAILY_LEVELS["nearest_resistance"] if l["level"] > price]
    sup = [(l["level"], l["label"]) for l in DAILY_LEVELS["nearest_support"]   if l["level"] < price]
    return {
        "resistance": sorted(res, key=lambda x: x[0])[:n],
        "support":    sorted(sup, key=lambda x: x[0], reverse=True)[:n],
    }


def get_confidence_modifier(price):
    d2u = DAILY_LEVELS["daily_2sd_upper"]
    d2l = DAILY_LEVELS["daily_2sd_lower"]
    d1u = DAILY_LEVELS["daily_1sd_upper"]
    d1l = DAILY_LEVELS["daily_1sd_lower"]
    if distance_to_level(price, d2u) <= 10 or distance_to_level(price, d2l) <= 10:
        return {"modifier": "CAUTION", "note": "Near 2SD extension - elevated reversal risk"}
    elif distance_to_level(price, d1u) <= 8:
        return {"modifier": "WATCH", "note": f"Approaching Daily 1SD Upper {d1u} - resistance ahead"}
    elif distance_to_level(price, d1l) <= 8:
        return {"modifier": "WATCH", "note": f"Approaching Daily 1SD Lower {d1l} - support nearby"}
    return {"modifier": "NORMAL", "note": ""}


def get_alert_context(price):
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
