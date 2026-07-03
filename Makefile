.PHONY: run test test-ftp test-p2p bench cert clean

PYTHON ?= python3
PYTHONPATH := src

# 启动服务器（可加参数，如: make run ARGS="--model select"）
run:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m pyftp_server $(ARGS)

# 端到端功能测试（FTP）
test: test-ftp

# FTP 端到端功能测试
test-ftp:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) tests/test_ftp.py

# 虫洞 P2P 直连模式测试
test-p2p:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) tests/test_p2p.py

# 三种并发模型性能对比
bench:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) tests/benchmark.py $(ARGS)

# 生成 FTPS 自签证书(--tls 启动前运行一次)
cert:
	mkdir -p certs
	openssl req -x509 -newkey rsa:2048 -keyout certs/server.key -out certs/server.crt \
		-days 365 -nodes -subj "/CN=pyftp"
	@echo "证书已生成: certs/server.crt + certs/server.key"

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name '*.py[co]' -delete
	find examples/ftproot -name 'benchmark_payload.bin' -delete
