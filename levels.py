# ─────────────────────────────────────────
# levels.py — SPX key levels for today
# Update this file each morning and upload to GitHub
# bot.py imports MANUAL_LEVELS automatically
# ─────────────────────────────────────────

# Date: April 22, 2026
# Source: John's level map + SPX VWAP chart

MANUAL_LEVELS = {
    "Daily VWAP 7105":       7105.50,  # Daily VWAP — key resistance
    "ATH VWAP 7103":         7103.69,  # ATH VWAP confluence
    "WTD VWAP 7095":         7095.16,  # WTD VWAP — pivot zone
    "Daily Level 7086":      7086.46,  # Daily support
    "WTD Low 7064":          7064.02,  # WTD low / orange VWAP
    "Daily 1SD Upper 7116":  7116.0,   # First upside target
    "Daily 1SD Lower 7012":  7012.0,   # Bear target
    "Weekly 1SD Lower 7017": 7017.0,   # Extended bear target
    "5 DMA 7070":            7070.0,   # Key bear support
    "Gamma Flip 6923":       6923.0,   # Major structural level
    "Round 7100":            7100.0,
    "Round 7000":            7000.0,
}
