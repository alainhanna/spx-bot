# ─────────────────────────────────────────
# levels.py — SPX key levels for today
# Update this file each morning and upload to GitHub
# bot.py imports MANUAL_LEVELS automatically
# ─────────────────────────────────────────

# Date: April 29, 2026
# Source: Godzilla Trader / GT level map

MANUAL_LEVELS = {
    # Expected moves — SPX at ~7,142
    "Daily 1SD Upper 7180":    7180.0,   # Bull target
    "Daily 1SD Lower 7097":    7097.0,   # Bear line — key support
    "Weekly 1SD Upper 7295":   7295.0,   # Weekly upper
    "Weekly 1SD Lower 7035":   7035.0,   # Weekly lower
    "5 DMA 7145":              7145.0,   # 5-day MA — right at price

    # Gamma flip
    "Gamma Flip 6998":         6998.0,   # Major structural level

    # Round numbers
    "Round 7100":              7100.0,
    "Round 7200":              7200.0,
}

