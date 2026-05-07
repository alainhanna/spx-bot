# ─────────────────────────────────────────
# levels.py — SPX key levels for today
# Format: "Level Name": (price, "PRIORITY")
# ─────────────────────────────────────────

# Date: Thursday May 7, 2026
# Source: Godzilla Trader / GT level map
# Pre-market spot: ~7,366 (new ATH territory, strong trend)

MANUAL_LEVELS = {
    # HIGH — structural + major VWAPs
    "Daily 1SD Upper 7409":  (7409.0,   "HIGH"),   # Bull target above
    "Daily 1SD Lower 7320":  (7320.0,   "HIGH"),   # Bear line / key support
    "Weekly 1SD Upper 7338": (7338.0,   "HIGH"),   # Clusters with Daily 1SD Lower area
    "5 DMA 7253":            (7253.0,   "HIGH"),   # Support far below
    "ATH VWAP 7365":         (7365.11,  "HIGH"),   # ATH VWAP — right at price
    "WTD VWAP 7347":         (7346.72,  "HIGH"),   # WTD VWAP — support below

    # MEDIUM — prior session VWAPs
    "VWAP 7275":             (7275.87,  "MEDIUM"), # Prior session
    "VWAP 7269":             (7269.14,  "MEDIUM"), # Prior session support

    # MEDIUM — round numbers
    "Round 7400":            (7400.0,   "MEDIUM"), # Round above
    "Round 7300":            (7300.0,   "MEDIUM"), # Round below

    # LOW — far levels
    "Gamma Flip 7236":       (7236.0,   "LOW"),    # Gamma flip below
    "Weekly 1SD Lower 7122": (7122.0,   "LOW"),    # Far bear target
}
