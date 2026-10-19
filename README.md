# fable-prop-sim

Paper-trading simulation engine for prop-firm style accounts (FTMO-type rulebooks). **Simulation only: no broker, no real orders, no money.**

The scheduled task that operates the simulation installs this package from GitHub pinned to a commit:

    pip install --no-deps --break-system-packages "git+https://github.com/<owner>/fable-prop-sim@<commit>"
    python3 -m fable_prop_sim --help

Commands: `fetch`, `portfolio-init`, `portfolio-run`, `portfolio-status`, `portfolio-export`, `portfolio-journal`, `portfolio-cancel`, `portfolio-backtest` (and the single-account `init/run/status/export/backtest`).
Data: Yahoo Finance chart feed (daily bars, hourly gold/silver). State: a JSON pack (manifest + one state per account) kept as an asset of the owner's dashboard artifact.
