PYTHON ?= python3
RUFF ?= ruff

JS_FILES := $(wildcard static/*.js)

.PHONY: check format format-check js-check lint test

check: lint format-check test js-check

format:
	$(RUFF) format .

format-check:
	$(RUFF) format --check .

lint:
	$(RUFF) check .

test:
	$(PYTHON) -m unittest discover -v

js-check:
	node --check $(JS_FILES)
