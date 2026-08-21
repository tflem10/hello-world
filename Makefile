.PHONY: install test lint scan backtest research schedule notify-test clean

VENV := .venv
PY   := $(VENV)/bin/python
SWING := $(VENV)/bin/swing

install:
	./install.sh

test:
	$(VENV)/bin/pytest

lint:
	$(VENV)/bin/ruff check src tests

scan:
	$(SWING) scan

backtest:
	$(SWING) backtest --walk-forward

research:
	$(SWING) backtest --ablations

schedule:
	$(SWING) schedule install

notify-test:
	$(SWING) notify-test

clean:
	rm -rf .pytest_cache .ruff_cache **/__pycache__ dist build
