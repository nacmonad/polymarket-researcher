VENV := /Users/pointcoexpedro/Dev/.venv
PYTHON := $(VENV)/bin/python
UV := uv

.PHONY: install install-ml collect backtest dashboard paper-trade migrate data-quality backfill help

help:
	@echo "polymarket-researcher"
	@echo ""
	@echo "  make install            Install core dependencies into ~/Dev/.venv"
	@echo "  make install-ml         Install ML extras (xgboost, optuna, plotly)"
	@echo "  make migrate            Apply DuckDB schema migrations"
	@echo "  make collect            Start data collection (oracle + PM CLOB)"
	@echo "  make backfill           Backfill last 24h of closed markets + outcomes"
	@echo "  make backfill HOURS=4   Backfill last 4 hours"
	@echo "  make backtest           Run backtesting framework"
	@echo "  make data-quality       Run data quality checks"
	@echo "  make dashboard          Open rich live dashboard (alias for collect)"
	@echo "  make paper-trade        Start paper trading engine"

install:
	$(UV) pip install -e . --python $(PYTHON)

install-ml:
	$(UV) pip install -e ".[ml]" --python $(PYTHON)

migrate:
	$(PYTHON) -m db.migrate

collect:
	$(PYTHON) collect.py

dashboard: collect

backtest:
	$(PYTHON) -m backtest.run

data-quality:
	$(PYTHON) scripts/data_quality.py

HOURS ?= 24
backfill:
	$(PYTHON) scripts/backfill_markets.py --hours $(HOURS)

paper-trade:
	$(PYTHON) -m paper_trader.engine
