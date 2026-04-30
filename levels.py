# ─────────────────────────────────────────
# levels.py — SPX key levels for today
# Update this file each morning and upload to GitHub
# bot.py imports MANUAL_LEVELS automatically
# ─────────────────────────────────────────

# Date: April 30, 2026
# Source: Godzilla Trader / GT level map

MANUAL_LEVELS = {
    # Expected moves — SPX at ~7,136
    "Daily 1SD Upper 7200":    7200.0,   # Bull target
    "Daily 1SD Lower 7072":    7072.0,   # Bear line
    "Weekly 1SD Upper 7295":   7295.0,   # Weekly upper
    "Weekly 1SD Lower 7035":   7035.0,   # Weekly lower
    "5 DMA 7144":              7144.0,   # 5-day MA — resistance above

    # VWAP cluster — top 2 only
    "WTD VWAP 7142":           7142.70,  # WTD VWAP — right at price
    "ATH VWAP 7135":           7135.96,  # ATH VWAP — key support

    # Prior session VWAPs
    "VWAP 7131":               7131.29,  # Prior session
    "VWAP 7129":               7129.77,  # Prior session support

    # Gamma flip
    "Gamma Flip 7017":         7017.0,   # Major structural level

    # Round numbers
    "Round 7100":              7100.0,
    "Round 7200":              7200.0,
}


