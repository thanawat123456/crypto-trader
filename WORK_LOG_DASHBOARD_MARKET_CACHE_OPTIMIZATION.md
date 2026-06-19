# Work Log: Dashboard Market Cache Optimization

Date: 2026-06-19

## Problem

The dashboard was slow to show the table and charts because it fetched market data repeatedly during one render.

## Root Cause

- The symbol table called market snapshot logic for every symbol.
- The split symbol chart section fetched OHLCV again for every symbol.
- The candlestick chart fetched OHLCV again for the selected symbol.
- The VM sync button cleared all Streamlit data cache, forcing a full market reload after every sync.

## Changes

- Reused one cached OHLCV dataset per symbol across the table, split charts, and candlestick chart.
- Removed the separate latest-price request and now uses the latest OHLCV close for dashboard display.
- Reused one cached Kraken exchange resource instead of rebuilding it for each request.
- Stopped clearing market cache automatically after syncing VM files.
- Added a separate sidebar button to clear market cache only when needed.

## Expected Impact

Cold load should drop from roughly 30+ Kraken calls to about one OHLCV call per watchlist symbol. Cached reruns should be much faster until the 5-minute cache TTL expires or the user manually clears market cache.

## Verification

- `.venv/bin/python -m py_compile dashboard.py`
- `python3 -m py_compile dashboard.py`
- Checked Streamlit cache API compatibility for `st.cache_resource(show_spinner=...)` and `st.cache_data.clear()`.
