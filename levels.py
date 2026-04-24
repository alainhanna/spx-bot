# ─────────────────────────────────────────────────────────────────────────────
# daily_levels.py  —  Friday, April 24, 2026
# Source: Godzilla pre-market levels chart + SPX VWAPs chart
# Role: CONTEXT ONLY — do not inject into key_levels or signal engine
# ─────────────────────────────────────────────────────────────────────────────

DAILY_LEVELS = {

    # ── Gamma / Structure ─────────────────────────────────────────────────────
    "gamma_flip":           6961,

    # ── Standard Deviation Bands ──────────────────────────────────────────────
    "daily_1sd_upper":      7161,
    "daily_1sd_lower":      7056,
    "daily_2sd_upper":      7213,
    "daily_2sd_lower":      7003,

    "weekly_1sd_upper":     7235,
    "weekly_1sd_lower":     7017,
    "weekly_2sd_upper":     7344,
    "weekly_2sd_lower":     6908,

    "monthly_1sd_upper":    6905,
    "monthly_2sd_upper":    7300,
    "monthly_1sd_lower":    6152,

    # ── Quarterly Levels ──────────────────────────────────────────────────────
    "q2_upper":             7195,
    "q2_lower":             5861,

    # ── Yearly Levels ─────────────────────────────────────────────────────────
    "yearly_1sd_upper":     8007,
    "yearly_2sd_upper":     9169,
    "yearly_1sd_lower":     5684,
    "yearly_2sd_lower":     4522,

    # ── Moving Averages ───────────────────────────────────────────────────────
    "dma": {
        5:   7108,
        20:  6805,
        50:  6777,
        100: 6836,
        200: 6703,
    },

    # ── VWAPs ─────────────────────────────────────────────────────────────────
    "vwap": {
        "WTD":          7111.25,
        "ATH":          7108.41,
        "Daily_1":      7107.81,
        "Daily_2":      7107.11,
        "MTD":          6888.95,
        "YTD_Low":      6842.47,
        "YTD":          6831.08,
        "Aug_Low":      6743.13,
        "May12_Gap":    6600.42,
        "Apr2025_Low":  6488.59,
        "VWAP_2025":    6351.84,
    },

    # ── Key Price Ladder ──────────────────────────────────────────────────────
    "key_price_ladder": {
        "ATH/WTD VWAP cluster":     7108,
        "Daily 1SD Lower":          7056,
        "Weekly 1SD Lower":         7017,
        "Gamma Flip":               6961,
        "Monthly 1SD Upper":        6905,
        "MTD VWAP":                 6889,
        "100 DMA":                  6836,
        "20 DMA":                   6805,
        "50 DMA":                   6777,
        "200 DMA":                  6703,
        "Daily 1SD Upper":          7161,
        "Q2 Upper":                 7195,
        "Daily 2SD Upper":          7213,
        "Weekly 1SD Upper":         7235,
        "Monthly 2SD Upper":        7300,
        "Weekly 2SD Upper":         7344,
    },

    # ── Session Bias ─────────────────────────────────────────────────────────
    "bias":     "BULLISH",
    "bias_note": (
        "Price coiling on ATH/WTD VWAP cluster (7108). 5 DMA at 7108. "
        "Godzilla: Hidden gem — STRONG BUY. Ascending trendline support intact. "
        "Hold 7108 -> 7161 Daily 1SD Upper. Lose 7108 -> 7056, then 7017. "
        "Gamma Flip 6961 must hold for bull thesis."
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# HELPER FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def get_bias(price: float) -> str:
    gf   = DAILY_LEVELS["gamma_flip"]
    d1l  = DAILY_LEVELS["daily_1sd_lower"]
    d1u  = DAILY_LEVELS["daily_1sd_upper"]
    vwap = DAILY_LEVELS["vwap"]["ATH"]

    if price >= d1u:
        return "EXTENDED BULLISH — near Daily 1SD Upper"
    elif price >= vwap:
        return "BULLISH — holding above ATH/WTD VWAP cluster"
    elif price >= d1l:
        return "CAUTIOUS BULLISH — between VWAP cluster and Daily 1SD Lower"
    elif price >= gf:
        return "BEARISH LEAN — below VWAP cluster, above Gamma Flip"
    else:
        return "BEARISH — below Gamma Flip (6961)"


def distance_to_level(price: float, level_name: str) -> float:
    ladder = DAILY_LEVELS["key_price_ladder"]
    if level_name not in ladder:
        raise KeyError(f"Level '{level_name}' not in key_price_ladder")
    return round(ladder[level_name] - price, 1)


def nearest_levels(price: float, n: int = 2):
    ladder = DAILY_LEVELS["key_price_ladder"]
    above = sorted([(v, k) for k, v in ladder.items() if v > price])[:n]
    below = sorted([(v, k) for k, v in ladder.items() if v < price], reverse=True)[:n]
    return {
        "resistance": [(k, v) for v, k in above],
        "support":    [(k, v) for v, k in below],
    }


def get_alert_context(price: float) -> str:
    bias = get_bias(price)
    lvls = nearest_levels(price, n=1)

    r_name, r_val = lvls["resistance"][0] if lvls["resistance"] else ("--", price)
    s_name, s_val = lvls["support"][0]    if lvls["support"]    else ("--", price)

    r_dist = round(r_val - price, 0)
    s_dist = round(price - s_val, 0)

    flag = ""
    if price >= DAILY_LEVELS["daily_2sd_upper"] or price <= DAILY_LEVELS["daily_2sd_lower"]:
        flag = " ⚠️ CAUTION: 2SD Extension"
    elif price >= DAILY_LEVELS["daily_1sd_upper"] or price <= DAILY_LEVELS["daily_1sd_lower"]:
        flag = " 👀 WATCH: 1SD Extension"

    return (
        f"Structure: {bias}{flag}\n"
        f"R: {r_name} {r_val} (+{r_dist:.0f}pt) | S: {s_name} {s_val} (-{s_dist:.0f}pt)"
    )
