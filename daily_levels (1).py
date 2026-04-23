# ─────────────────────────────────────────────────────────────────────────────
# daily_levels.py  —  Thursday, April 23, 2026
# Source: Godzilla / Godzichartslla pre-market levels chart
# Role: CONTEXT ONLY — do not inject into key_levels or signal engine
# ─────────────────────────────────────────────────────────────────────────────

DAILY_LEVELS = {

    # ── Gamma / Structure ─────────────────────────────────────────────────────
    "gamma_flip":           6961,   # Gamma Flip — bull/bear line in the sand

    # ── Standard Deviation Bands ──────────────────────────────────────────────
    "daily_1sd_upper":      7180,
    "daily_1sd_lower":      7095,
    "daily_2sd_upper":      7223,
    "daily_2sd_lower":      7052,

    "weekly_1sd_upper":     7235,
    "weekly_1sd_lower":     7017,
    "weekly_2sd_upper":     7344,
    "weekly_2sd_lower":     6908,

    "monthly_1sd_upper":    6905,   # Monthly 1SD Upper
    "monthly_2sd_upper":    7300,
    "monthly_1sd_lower":    6152,   # Monthly 1SD Lower (from chart annotation)

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
        5:   7095,   # 5 DMA (confluent with Daily 1SD Lower)
        20:  6773,
        50:  6774,
        100: 6833,
        200: 6698,
    },

    # ── Key Price Ladder (nearest actionable levels) ───────────────────────────
    "key_price_ladder": {
        "Daily 1SD Lower / 5 DMA":  7095,   # Decision zone — must hold for bulls
        "Weekly 1SD Lower":         7017,   # First real support below 7095
        "Gamma Flip":               6961,   # Bull thesis invalidated below here
        "Monthly 1SD Upper":        6905,
        "100 DMA":                  6833,
        "50 DMA":                   6774,
        "20 DMA":                   6773,
        "200 DMA":                  6698,
        "Daily 1SD Upper":          7180,   # Upside target if 7095 holds
        "Weekly 1SD Upper":         7235,
        "Daily 2SD Upper":          7223,
        "Monthly 2SD Upper":        7300,
        "Weekly 2SD Upper":         7344,
        "Q2 Upper":                 7195,
    },

    # ── Session Bias ─────────────────────────────────────────────────────────
    "bias":     "BULLISH",          # Above gamma flip, broad participation noted
    "bias_note": (
        "Price above Gamma Flip (6961) — bullish structure intact. "
        "Decision point at Daily 1SD Lower / 5 DMA (7095). "
        "Hold above 7095 → path to Daily 1SD Upper (7180). "
        "Fail 7095 → Weekly 1SD Lower (7017) next, then Gamma Flip (6961) as last bull line."
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# HELPER FUNCTIONS  (context layer — read-only, no signal generation)
# ─────────────────────────────────────────────────────────────────────────────

def get_bias(price: float) -> str:
    """Return directional bias string based on current price vs key levels."""
    gf   = DAILY_LEVELS["gamma_flip"]
    d1l  = DAILY_LEVELS["daily_1sd_lower"]
    d1u  = DAILY_LEVELS["daily_1sd_upper"]

    if price >= d1u:
        return "EXTENDED BULLISH — near Daily 1SD Upper"
    elif price >= d1l:
        return "BULLISH — holding above Daily 1SD Lower / 5 DMA"
    elif price >= gf:
        return "CAUTIOUS BULLISH — between Gamma Flip and Daily 1SD Lower"
    else:
        return "BEARISH — below Gamma Flip (6961)"


def distance_to_level(price: float, level_name: str) -> float:
    """Return signed distance (points) from price to a named level."""
    ladder = DAILY_LEVELS["key_price_ladder"]
    if level_name not in ladder:
        raise KeyError(f"Level '{level_name}' not in key_price_ladder")
    return round(ladder[level_name] - price, 1)


def nearest_levels(price: float, n: int = 2):
    """Return (n) nearest resistance and (n) nearest support levels."""
    ladder = DAILY_LEVELS["key_price_ladder"]
    above = sorted([(v, k) for k, v in ladder.items() if v > price])[:n]
    below = sorted([(v, k) for k, v in ladder.items() if v < price], reverse=True)[:n]
    return {
        "resistance": [(k, v) for v, k in above],
        "support":    [(k, v) for v, k in below],
    }


def get_alert_context(price: float) -> str:
    """
    Returns a compact market-structure line to append to Telegram alerts.
    Example: "Structure: BULLISH | R: Daily 1SD Upper 7180 (+73pt) | S: Weekly 1SD Lower 7017 (-78pt)"
    """
    bias = get_bias(price)
    lvls = nearest_levels(price, n=1)

    r_name, r_val = lvls["resistance"][0] if lvls["resistance"] else ("—", price)
    s_name, s_val = lvls["support"][0]    if lvls["support"]    else ("—", price)

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
