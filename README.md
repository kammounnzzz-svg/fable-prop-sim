# fable-prop-sim

Paper-trading simulation of a portfolio of prop-firm style accounts (FTMO-type rulebooks), written by Claude for its owner's "Fable Trading" project. **Simulation only: no broker, no real orders, no money.**

## How it runs

- `.github/workflows/sim.yml` runs `scripts/run_sim.py` every three hours Monday–Saturday (00:23, 03:23 … 21:23 UTC) on GitHub Actions, and on demand (workflow_dispatch).
- `scripts/run_sim.py` fetches price bars from the Yahoo Finance chart feed (daily bars for 30 markets, hourly bars for gold and silver), runs every account in `state/portfolio_state.json` through the engine (`python3 -m fable_prop_sim portfolio-run`), exports the dashboard datasets to `out/` and always writes `out/run_meta.json` (what happened: new events, closed trades, phase changes, fetch failures, errors) and `out/run_log.txt`.
- The workflow then commits `state/` and `out/` back to this repository, so every run is a commit and the full history of the simulation is the git history.
- A separate scheduled Claude task reads `out/` from the latest commit (data only), publishes the datasets to the owner's dashboard and sends a short report. It never executes anything from this repository.

## Layout

- `fable_prop_sim/engine.py` — the engine (standard library only). Commands: `fetch`, `portfolio-init`, `portfolio-run`, `portfolio-status`, `portfolio-export`, `portfolio-journal`, `portfolio-cancel`, `portfolio-backtest`, plus the single-account `init/run/status/export/backtest`.
- `state/portfolio_state.json` — the live state pack: `manifest` (accounts, copies, rulebook overrides, focus account) + one engine state per account. The manifest is the source of truth for the rules; every run rebuilds each account's config from it.
- `out/` — dashboard datasets for the focus account (account, positions, orders, trades, equity, events, history, watchlist, journal) plus `portfolio.json` (one row per account), `psummary.json` (portfolio totals), `run_meta.json`, `run_log.txt`, `run_history.json`.
- `portfolio.example.json` — an example manifest.

## Running it yourself

    PYTHONPATH=. python3 -m fable_prop_sim fetch --out bars --range 2y --hourly-range 60d
    PYTHONPATH=. python3 -m fable_prop_sim portfolio-run --states state/portfolio_state.json --bars bars
    PYTHONPATH=. python3 -m fable_prop_sim portfolio-status --states state/portfolio_state.json --bars bars --account ftmo-200k

or simply `python3 scripts/run_sim.py`.
