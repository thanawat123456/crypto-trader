# Work Log: Dashboard Split Symbol Charts

Date: 2026-06-19

## Request

Show each coin in its own chart instead of combining all symbols into one market movement chart.

## Changes

- Replaced the normalized combined market movement chart with separate close-price charts per symbol.
- Rendered charts in a two-column layout so every watchlist symbol has its own graph.
- Kept the change read-only; no trading logic was modified.

## Verification

- `.venv/bin/python -m py_compile dashboard.py`
