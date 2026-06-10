.PHONY: check build deploy clean

PYTHON ?= python3

check:
	$(PYTHON) -m py_compile gtktube/app.py gtktube/install_deps.py gtktube/ui/main_window.py
	$(PYTHON) -m unittest discover -s tests

build: clean
	$(PYTHON) -m build

deploy: check build
	$(PYTHON) -m twine upload dist/*

clean:
	rm -rf build dist *.egg-info
