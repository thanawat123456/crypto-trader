# Global BTC/ETH forecast benchmark — research only

This is a new non-commercial, personal, non-production **reference research** project, not a Binance TH execution model, profit trial, cloud collector migration or deployment. Main, V1, the running Mac shadow, its frozen core/config/protocol and all legacy holdout registrations remain unchanged. No credentials, balances, order APIs or local historic/account files are uploaded.

## Provider terms and attribution

Source: [Binance Vision / Binance public data](https://github.com/binance/binance-public-data). Not endorsed by Binance. The [dataset terms](https://github.com/binance/binance-public-data/blob/master/TERMS_AND_CONDITIONS.md), version 1.0 dated 2026-08-26, checked 2026-10-03, permit non-production research but restrict live/commercial use. Data and derived models/reports are **CC BY-NC-SA 4.0 with the provider's terms**, not the repository's source-code license. Preserve attribution and share-alike obligations. **None of these data/models may be routed to live trading.** The user acknowledged this restriction before the run. A future production model requires an appropriately permitted dataset and separate approval/evidence.

## Fixed input plan and safety envelope

- Spot monthly BTCUSDT and ETHUSDT H4 klines, 2018-01-01 through **before 2026-06-01 UTC**: 202 archives and their official CHECKSUM files, 404 planned GETs. This excludes the June–October native protected period.
- Allowlisted archive URLs only; no redirects, authorization, proxies, alternate sources, retries or gap-filling. Caps: 420 attempted requests, 16 MiB download, 1 MiB per object/uncompressed CSV, 900 seconds downloading. Failed attempts consume the budget; successful raw responses and partial-failure receipts are retained.
- Validate official ZIP SHA256, exact member/path/count, CRC, 12 spot columns, prices/quantities, millisecond timestamps before 2025 and microseconds from 2025, H4 grid and close time, chronological uniqueness and month bounds. Export deterministic CSV. Re-audit raw-to-CSV before training.
- Use the longest common **continuous** BTC/ETH segment, earliest tie. Selection is based only on timestamps/coverage, never returns; no synthetic bars or bridging gaps. Insufficient continuous history stops the run.
- Register archive plan, features, parameters, scope, dataset license, core/source hashes and GitHub provenance **before HTTP reads**. Core stays `fb02d4e3de3caa0cded139c25f16bceaaccade804f82c0473ff1b5605403723b`. Recheck bindings at stage boundaries.
- At most 31 folds / 62 candidate models and 600 seconds fitting. Job timeout 25 minutes, fresh output only, one attempt, expires 2026-10-10 UTC. Public artifact cap 64 MiB (with reserved inventory space), retained 3 days. No account billing/settings changes, larger runners, cache or recurring schedule.

## Forecast hypothesis, not trading PnL

This is deliberately a **different hypothesis** from the existing breakout-only entry model. Dense daily samples from H4 closed bars, with 210-bar warmup, supply 18 causal features: lagged returns, simultaneous other-asset returns, realized volatility, ATR, volume ratio/missing flag, price-path efficiency, range position, MA200 distance/slope and symbol flags. Two candidates are fixed in advance: weighted regularized logistic/ridge, and the same model with five predefined interaction products. No hyperparameter search after validation results.

Outcome: fixed seven-day long from the next opening to the opening 42 H4 bars later. Quote-cost arithmetic assumes 0.1% fee per leg, 0.1% spread, 0.05% slippage per leg and an additional 0.1% per leg in stress. These are **unverified reference assumptions**, not historical executable quotes, native venue terms, actual account fees or broker fills. It is not a ledger. Overlapping weekly labels, two correlated assets and rolling windows do not constitute independent trades.

Chronological windows: 24 months fit → 12 months probability calibration → 3 months research shortlist → 2 months validation, stepping 2 months. Globally purge labels across both assets at every boundary, with one H4-bar embargo. Fit-only scalers and constant predictors, inverse-overlap weights, existing support/class minima and calibration-domain/OOD guards are retained. Models expire after 180 days; gates require an in-date calibrated model, in-domain features, probability ≥0.60 and predicted stress return >0.25%.

Approval is a **forecast-only research shortlist**, not the legacy trading approval: minimum 20 labels and 10 gate passes, positive mean gate-pass stress proxy label, Brier and log loss no worse than the fit-only constant prior. Fixed candidate priority, otherwise cash. Lock that choice before examining validation scores. Record results for both candidates, including failures, without promoting or retuning anything. Compare return MAE against the **fit-only** stress-return mean, never the validation mean.

## Evidence classification and verification

All downloaded history is **retrospective development**, including previously inspected and correlated periods. Adding a venue does not turn old/correlated data into independent unseen OOS. Legacy Kraken/native holdouts are untouched; this separate reference scope neither consumes nor reclassifies them as fresh profit evidence.

Artifacts include raw ZIP/checksums/receipts, CSV and coverage manifest, immutable registrations, features/outcomes, each model/input hash, approval/validation predictions, locked selections, fold reports, top-level result, attribution notice and a complete file inventory. Read-only audit rebuilds causal inputs, horizon purges, fit-only scalers/constants, predictions, scores and selections **without refitting**; hashes bind exact saved inputs while narrow numerical tolerance accommodates Linux/macOS floating reductions. Audits establish consistency, not source immutability forever or profitability.

The standalone schema `global-reference-forward-forecast-v1` has no execution adapter and is not accepted by the native `NetEntryModel` loader. Every run/model/selection/report carries no-live flags and the provider license. Even excellent forecast scores cannot satisfy portfolio risk, stop/sizing, dust/minimums, native spreads/liquidity, realistic fills or forward-net-profit requirements. No screenshots, win rate or proxy mean alone establish sustainable profit.

## Execution

Branch: `codex/v2-global-ml-20261003`; workflow `.github/workflows/v2-global-ml.yml` triggers only a push to that branch, after all offline tests pass. No main merge or scheduler changes. Standard GitHub-hosted public-repository runners are used; storage allowances are shared with other account use, so bounded retention is not a guarantee about the account's total bill.

Read-only downloaded-artifact audit:

```sh
.venv/bin/python -B -m scripts.github_global_research --audit-only --output /absolute/new-downloaded-artifact
```

Run URLs, actual checksums/counts, audit outcomes and benchmark findings must be added to the local work log **only after they exist**, not inferred from successful synthetic tests.

### Linux correctness portability correction

The initial published run `37141040733` stopped at offline tests (344 tests, one legacy shadow snapshot assertion failed); the data/training job was skipped, so no research data was downloaded or scored. The legacy test compared SQLite reader coordination bytes as though they were ledger writes. The revised test masks only the documented 20-byte read-mark region at offsets 100..119 in the two exact ledger `-shm` files, per [SQLite WAL format](https://www.sqlite.org/walformat.html). It still binds every database/WAL/raw byte, file set, all other shared-memory header/index bytes and independently replayed ledger content hashes; an added regression tests both permitted reader marks and forbidden changes. No runtime/core code, thresholds, model parameters or market results changed. The failed run is retained; the source correction produces a distinct commit/run, not an automatic rerun or new independent financial evidence.
