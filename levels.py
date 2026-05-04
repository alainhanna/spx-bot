# ─────────────────────────────────────────
# levels.py — SPX key levels for today
# Update this file each morning and upload to GitHub
# bot.py imports MANUAL_LEVELS automatically
#
# Format: "Level Name": (price, "PRIORITY")
# Priorities: HIGH, MEDIUM, LOW
#   HIGH   — Daily 1SD, major VWAPs (ATH, WTD)        → zone width 6pts
#   MEDIUM — Prior session VWAPs, round numbers        → zone width 4pts
#   LOW    — Distant levels (gamma flip, yearly bands) → zone width 3pts
# ─────────────────────────────────────────

# Date: Monday May 4, 2026
# Source: Godzilla Trader / GT level map
# Pre-market spot: ~7,325 (gap up)

MANUAL_LEVELS = {
    # HIGH — structural + major VWAPs
    "Daily 1SD Upper 7279":  (7279.0,   "HIGH"),
    "Daily 1SD Lower 7181":  (7181.0,   "HIGH"),
    "5 DMA 7177":            (7177.0,   "HIGH"),
    "ATH VWAP 7246":         (7246.69,  "HIGH"),
    "WTD VWAP 7173":         (7173.43,  "HIGH"),

    # MEDIUM — prior session VWAPs + round numbers
    "VWAP 7230":             (7230.11,  "MEDIUM"),
    "VWAP 7212":             (7212.51,  "MEDIUM"),
    "Round 7300":            (7300.0,   "MEDIUM"),
    "Round 7200":            (7200.0,   "MEDIUM"),

    # MEDIUM — weekly structure
    "Weekly 1SD Lower 7122": (7122.0,   "MEDIUM"),

    # LOW — far levels
    "Gamma Flip 6957":       (6957.0,   "LOW"),
}
