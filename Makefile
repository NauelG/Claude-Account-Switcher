PYTHON ?= python3

.PHONY: test install

test:
	@set -eu; for test in tests/test_*.py; do $(PYTHON) -B "$$test"; done

install:
	$(PYTHON) install.py
