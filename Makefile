.PHONY: install test lint typecheck run-dashboard

PYTHON ?= python3
PIP ?= $(PYTHON) -m pip

install:
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check .

typecheck:
	$(PYTHON) -m mypy

run-dashboard:
	$(PYTHON) -m streamlit run src/dashboard/app.py
