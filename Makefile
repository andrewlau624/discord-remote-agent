.PHONY: run install clean

VENV := .venv
PY := $(VENV)/bin/python

$(PY):
	python3 -m venv $(VENV)

install: $(PY)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt

run: $(PY)
	$(PY) run.py

clean:
	rm -rf **/__pycache__ __pycache__ *.pyc
