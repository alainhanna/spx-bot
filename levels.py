# ─────────────────────────────────────────
# levels.py — SPX key levels for today
# Update this file each morning and upload to GitHub
# bot.py imports MANUAL_LEVELS automatically
# ─────────────────────────────────────────

# Date: May 1, 2026
# Source: Godzilla Trader / GT level map

MANUAL_LEVELS = {
    # Expected moves — SPX at ~7,222
    "Daily 1SD Upper 7250":    7250.0,   # Bull target — right above price
    "Daily 1SD Lower 7168":    7168.0,   # Bear line / key support
    "Weekly 1SD Upper 7295":   7295.0,   # Weekly upper
    "Weekly 1SD Lower 7035":   7035.0,   # Weekly lower
    "5 DMA 7164":              7164.0,   # 5-day MA — support below

    # VWAP cluster — top 2 only
    "ATH VWAP 7212":           7212.60,  # ATH VWAP — just below price
    "WTD VWAP 7209":           7209.02,  # WTD VWAP

    # Prior session VWAPs — 2 most relevant
    "VWAP 7186":               7186.94,  # Prior session
    "VWAP 7155":               7155.71,  # Prior session support

    # Gamma flip
    "Gamma Flip 7017":         7017.0,   # Major structural level

    # Round numbers
    "Round 7200":              7200.0,
    "Round 7300":              7300.0,
}



