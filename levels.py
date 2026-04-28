# ─────────────────────────────────────────
# levels.py — SPX key levels for today
# Update this file each morning and upload to GitHub
# bot.py imports MANUAL_LEVELS automatically
# ─────────────────────────────────────────

# Date: April 28, 2026
# Source: Godzilla Trader / GT level map

MANUAL_LEVELS = {
    # Expected moves — SPX at ~7,156
    "Daily 1SD Upper 7213":    7213.0,   # Bull target
    "Daily 1SD Lower 7135":    7135.0,   # Key support / bear line
    "Weekly 1SD Upper 7295":   7295.0,   # Weekly upper
    "Weekly 1SD Lower 7035":   7035.0,   # Weekly lower
    "5 DMA 7130":              7130.0,   # 5-day MA — critical support

    # VWAP cluster — top 2 only
    "WTD VWAP 7173":           7173.92,  # WTD VWAP — resistance
    "ATH VWAP 7166":           7166.46,  # ATH VWAP

    # Prior session VWAPs (2 most relevant)
    "VWAP 7151":               7151.17,  # Prior session
    "VWAP 7143":               7143.15,  # Prior session support

    # Gamma flip
    "Gamma Flip 6998":         6998.0,   # Major structural level

    # Round numbers
    "Round 7100":              7100.0,
    "Round 7200":              7200.0,
}
