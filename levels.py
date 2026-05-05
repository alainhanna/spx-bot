# ─────────────────────────────────────────
# levels.py — SPX key levels for today
# Format: "Level Name": (price, "PRIORITY")
# ─────────────────────────────────────────

# Date: Tuesday May 5, 2026
# Source: Godzilla Trader / GT level map
# Pre-market spot: ~7,206 (pullback from Monday gap)

MANUAL_LEVELS = {
    # HIGH — structural + major VWAPs
    "Daily 1SD Upper 7245":  (7245.0,   "HIGH"),   # Bull target
    "Daily 1SD Lower 7156":  (7156.0,   "HIGH"),   # Bear line — key support
    "5 DMA 7183":            (7183.0,   "HIGH"),   # Clusters with Daily 1SD Lower area
    "Weekly 1SD Lower 7122": (7122.0,   "HIGH"),   # Bear target if 7156 fails
    "ATH VWAP 7225":         (7225.81,  "HIGH"),   # ATH VWAP — resistance above

    # MEDIUM — prior session VWAPs (tight cluster near price)
    "WTD VWAP 7200":         (7200.76,  "MEDIUM"), # WTD VWAP — right at price
    "VWAP 7205":             (7205.26,  "MEDIUM"), # Prior session
    "VWAP 7210":             (7210.31,  "MEDIUM"), # Prior session resistance
    "VWAP 7190":             (7190.11,  "MEDIUM"), # Prior session support

    # MEDIUM — round numbers / GEX levels
    "Round 7200":            (7200.0,   "MEDIUM"), # GEX put support / round
    "Round 7300":            (7300.0,   "MEDIUM"), # GEX call resistance

    # LOW — far levels
    "Gamma Flip 7042":       (7042.0,   "LOW"),    # Gamma flip
}
