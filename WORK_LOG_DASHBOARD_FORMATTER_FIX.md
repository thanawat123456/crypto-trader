# Work Log: Dashboard Formatter Fix

Date: 2026-06-19

## Problem

Streamlit crashed with `TypeError: unsupported format string passed to NoneType.__format__` when the performance summary table tried to format missing numeric values.

## Root Cause

Some dashboard rows can have `None` for values like `unrealized` when there is no open position or market price. Pandas Styler does not accept numeric format strings for raw `None` values unless they are coerced to numeric missing values first.

## Changes

- Coerce numeric dashboard columns with `pd.to_numeric(..., errors="coerce")` before styling.
- Added `na_rep="-"` to the per-symbol performance summary formatter.
- Applied the same numeric coercion to the main symbol table styling path.

## Verification

- `.venv/bin/python -m py_compile dashboard.py`
- Smoke-tested Pandas Styler translation with a `None` unrealized value.
