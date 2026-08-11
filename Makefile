.PHONY: run install clean

run:
	python3 run.py

install:
	python3 -m pip install -r requirements.txt

clean:
	rm -rf **/__pycache__ __pycache__ *.pyc
