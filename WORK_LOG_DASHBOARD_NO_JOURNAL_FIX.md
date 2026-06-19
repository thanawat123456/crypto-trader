# Work Log: Dashboard No-Journal Fix

Date: 2026-06-19

## Problem

The dashboard looked empty or broken when the GCP VM did not have `trade_journal.csv` yet. This is expected during early paper trading if no trades have been written, but the UI did not make that clear enough.

## Changes

- Treat missing optional VM files (`trade_journal.csv`, `validation_log.csv`) as normal dashboard status instead of sync failure.
- Added a market movement chart for every watchlist symbol so the dashboard still shows useful graphs before any trades exist.
- Added a fallback close-price line chart if Plotly candlestick rendering fails.
- Added a validation log section that appears when `validation_log.csv` exists and explains the empty state when it does not.
- Improved the empty trade journal message to explain that no journal can be normal before the bot has BUY/SELL records.

## Verification

- `.venv/bin/python -m py_compile dashboard.py`
- `python3 -m py_compile dashboard.py`

## Notes

This change is read-only for trading behavior. It only improves the local Streamlit dashboard display and VM sync messaging.
