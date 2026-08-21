.PHONY: run install test clean

VENV := .venv
PY := $(VENV)/bin/python

$(PY):
	python3 -m venv $(VENV)

install: $(PY)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt

run: $(PY)
	$(PY) run.py

# Installs whatever is missing rather than assuming `make install` has run.
test: $(PY)
	@$(PY) -c "import discord" 2>/dev/null || $(MAKE) install
	@$(PY) -c "import pytest" 2>/dev/null || $(PY) -m pip install -q -r requirements-dev.txt
	$(PY) -m pytest -q

clean:
	rm -rf **/__pycache__ __pycache__ *.pyc .pytest_cache
