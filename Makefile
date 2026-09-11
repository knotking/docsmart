VENV := .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

.PHONY: install test api web demo corpus scan clean fmt

install:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"
	cd web && npm install

test:
	$(VENV)/bin/pytest -q --cov=termguard --cov-report=term-missing

api:
	$(VENV)/bin/uvicorn termguard.api:app --reload --port 8000

web:
	cd web && npm run dev

corpus:
	$(PY) scripts/make_corpus.py

scan:
	$(PY) scripts/scan.py

demo:
	$(PY) scripts/demo.py

clean:
	rm -rf data/out/* data/blobs/* termguard.db .docx-editor-workspaces
