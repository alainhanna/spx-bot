# ─────────────────────────────────────────
# levels.py — SPX key levels for today
# Update this file each morning and upload to GitHub
# bot.py imports MANUAL_LEVELS automatically
# ─────────────────────────────────────────

# Date: April 27, 2026
# Source: Godzilla Trader / GT level map

MANUAL_LEVELS = {
    # GEX levels (0DTE expiration Apr 27)
    "Call Wall 7130":          7130.0,   # GEX call resistance — key ceiling
    "HVL 7095":                7095.0,   # High volume level — magnet/pivot
    "Put Support 7055":        7055.0,   # GEX put floor

    # Expected moves
    "Daily 1SD Upper 7224":    7224.0,   # Bull target extended
    "Daily 1SD Lower 7106":    7106.0,   # Bear target / key support
    "Weekly 1SD Upper 7295":   7295.0,   # Weekly upper
    "Weekly 1SD Lower 7035":   7035.0,   # Weekly lower
    "5 DMA 7117":              7117.0,   # 5-day moving average

    # VWAP cluster — top 2 only, avoid compression bonus inflation
    "ATH VWAP 7165":           7165.07,  # ATH VWAP — upper target
    "VWAP 7139":               7139.96,  # Prior session VWAP

    # Gamma flip
    "Gamma Flip 6950":         6950.0,   # Major structural level

    # Round numbers
    "Round 7100":              7100.0,
    "Round 7200":              7200.0,
}


