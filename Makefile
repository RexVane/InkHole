.PHONY: test clean

PYTHON ?= python3
PYTHONPATH := src

# P2P 端到端测试
test:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) tests/test_p2p.py

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name '*.py[co]' -delete
