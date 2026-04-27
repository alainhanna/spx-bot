# ─────────────────────────────────────────
# levels.py — SPX key levels for today
# Update this file each morning and upload to GitHub
# bot.py imports MANUAL_LEVELS automatically
# ─────────────────────────────────────────

# Date: April 27, 2026
# Source: Godzilla Trader / GT level map

MANUAL_LEVELS = {
    # GEX levels (0DTE expiration Apr 27)
    "Call Wall 7130":        7130.0,   # GEX call resistance — key ceiling
    "HVL 7095":              7095.0,   # High volume level — magnet/pivot
    "Put Support 7055":      7055.0,   # GEX put floor

    # VWAP cluster
    "ATH VWAP 7165":         7165.07,  # ATH VWAP — upper target if 7130 breaks
    "VWAP 7139":             7139.96,  # Prior session VWAP
    "VWAP 7130":             7130.06,  # Key orange VWAP confluence w/ call wall
    "VWAP 7119":             7119.06,  # Lower session VWAP — bear/bull dividing line

    # Round numbers
    "Round 7100":            7100.0,
    "Round 7200":            7200.0,
}

