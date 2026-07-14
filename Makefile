.PHONY: test clean

# Windows(Git Bash) 没有 python3 命令，自动回退到 python
PYTHON ?= $(shell command -v python3 2>/dev/null || echo python)
PYTHONPATH := src

# 全量测试(P2P 引擎 + 手动设备 + 主窗口离屏冒烟)
test:
	PYTHONPATH=$(PYTHONPATH) QT_QPA_PLATFORM=offscreen $(PYTHON) -m pytest tests/ -q

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name '*.py[co]' -delete
