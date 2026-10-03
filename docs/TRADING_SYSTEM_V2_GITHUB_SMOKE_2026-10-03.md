# V2 GitHub Linux smoke — 2026-10-03

Target: `thanawat123456/crypto-trader`. The repository is public; the user
explicitly confirmed publishing V2 source and synthetic tests. Account data,
credentials, historical study artifacts and local SQLite files are excluded.

## Scope

- New branch: `codex/v2-github-smoke-20261003`; no edits to remote `main`.
- New workflow: `.github/workflows/v2-github-smoke.yml`. A push to this exact
  branch triggers the test; manual dispatch is also declared. No schedule,
  pull-request trigger, private API, Discord message, V1 bot or real order.
- Job 1: all V2 offline correctness tests and a synthetic software demo.
- Job 2: only after job 1 passes, one public Binance TH BTC/ETH snapshot:
  212 closed H4 bars each, 16 successful requests at most, 2 MiB downloaded
  at most, 90-second feed budget, no HTTP retries or fallback exchange.
- Independent raw-response/candle/quote/market-rule audit. A failure is not
  reported as success; successful partial receipts are retained on failure.
- Standard `ubuntu-latest`, 10-minute correctness and 5-minute public job
  timeouts. Only `contents: read`; checkout does not persist credentials.
  Official checkout/setup-python/upload-artifact actions are pinned to commit
  SHAs verified against their official GitHub tag API responses.
- Only newly generated public smoke output is uploaded, with three-day
  retention. No existing `v2_data/` folder or portfolio is uploaded/restored.
- Script rejects a different repository/ref/event, rerun attempt >1,
  changed fixed capture budget, existing output or use after
  `2026-10-10T00:00:00Z`, before public HTTP reads.

The workflow is a short Linux/network portability check. `verified` means raw
data matches the export, NOT profitability, ML readiness or live approval.
It never opens a portfolio, trains/selects models or migrates local shadow
state. Local runs explicitly identify themselves as local, not GitHub runs.

## Deployment boundaries

The existing V1 workflow `.github/workflows/bot.yml` must remain byte-for-byte
unchanged. Existing V2 files are currently uncommitted user work; prepare the
test commit from an isolated temporary clone and an explicit source allowlist,
without staging/committing unrelated edits in the primary workspace.

The seven-day Mac shadow and its frozen runtime remain running and unchanged.
Do NOT switch off the Mac on the strength of this smoke. Scheduled GitHub
collection still needs provenance-preserving state portability, durable
cross-job checkpoints/reservations, missed-slot accounting, quota checks and
an explicitly approved single-writer handoff. GitHub scheduled events can be
delayed or dropped; Actions is not a continuously running server.

## Status

Local smoke regression tests: 11 passed. The isolated clone's first full-suite
run found a missing export dependency: `config.xau.research.template.json`
(the fail-closed unknown-contract fixture). Add this non-secret template to
the source allowlist; do not remove or weaken the test.

GitHub read access succeeded and remote `HEAD` matched local base commit
`66cb34b5c44a72603f1325502deac555b24c8e50`. A non-interactive push dry-run
from both the isolated clone and primary workspace failed with
`fatal: unable to get password from user`. No branch was pushed and no remote
workflow ran. The GitHub integration was not connected; do not collect tokens
or claim remote test success. User connection is needed before publication.

After adding that template, the isolated clone's complete suite passed:
301 tests in 76.268 seconds. The offline 600-bar synthetic demo also exited
successfully; its returns are software-fixture output, not profit evidence.
The 11 smoke-specific tests validate the workflow structure, immutable output,
public-only reads, pre-request registration, fail-closed budgets/context/expiry,
source mutation checks, credential exclusion and partial-evidence retention.

Pending: authenticated publication of the new test branch and inspection of
the actual remote Actions result. No schedule or Mac handoff is enabled.

Official references:

- [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [Scheduled workflow behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [Binance TH public API](https://www.binance.th/api-docs/en/)
