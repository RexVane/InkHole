.PHONY: test test-python test-core clean

# Windows(Git Bash) 没有 python3 命令，自动回退到 python；本地优先使用项目虚拟环境
PYTHON ?= $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; elif command -v python3 >/dev/null 2>&1; then command -v python3; else echo python; fi)
PYTHONPATH := src

# Python 桌面测试(P2P 引擎 + 手动设备 + 主窗口离屏冒烟)
test:
	$(MAKE) test-python
	$(MAKE) test-core

test-python:
	PYTHONPATH=$(PYTHONPATH) QT_QPA_PLATFORM=offscreen $(PYTHON) -m pytest tests/ -q

test-core:
	$(MAKE) -C transport-core test

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name '*.py[co]' -delete
