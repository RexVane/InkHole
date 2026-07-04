.PHONY: test clean

# Windows(Git Bash) 没有 python3 命令，自动回退到 python
PYTHON ?= $(shell command -v python3 2>/dev/null || echo python)
PYTHONPATH := src

# P2P 端到端测试
test:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) tests/test_p2p.py

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name '*.py[co]' -delete
