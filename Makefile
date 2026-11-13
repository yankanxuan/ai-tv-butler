# AI 电视管家常用命令
PYTHON ?= python3
.PHONY: help demo test e2e doctor mcp serve clean link snapshot

help:
	@echo "make demo     # 内置模拟电视全链路演示（无需真机）"
	@echo "make test     # 22 项集成检查 + 单元测试（含 Pydantic/会话/TTS）"
	@echo "make e2e      # 真实浏览器 e2e（需 Playwright + Chromium，测试依赖）"
	@echo "make doctor   # 自检 adb / 设备 / 大模型"
	@echo "make mcp      # 以 MCP stdio 服务运行"
	@echo "make serve    # 启动认证控制台（默认 127.0.0.1:8770）"
	@echo "make clean    # 仅清理 Python 字节码缓存，保留全部运行数据"
	@echo "make link     # 把 ai-tv 软链到 ~/.local/bin"
	@echo "make snapshot # 创建可回滚源码快照（指定 VERSION=x.y.z）"

demo:
	./bin/ai-tv demo

e2e:
	@echo "需测试依赖：pip install -r requirements-test.txt && playwright install chromium"
	$(PYTHON) -m unittest tests.e2e.test_voice_ui -v

test:
	$(PYTHON) tests/test_suite.py
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py'

doctor:
	./bin/ai-tv doctor

mcp:
	./bin/ai-tv mcp

serve:
	./bin/ai-tv serve

clean:
	python3 scripts/clean_cache.py

link:
	mkdir -p $(HOME)/.local/bin
	ln -sf $(CURDIR)/bin/ai-tv $(HOME)/.local/bin/ai-tv

snapshot:
ifndef VERSION
	@echo "用法: make snapshot VERSION=3.0.1"; exit 1
endif
	python3 scripts/release.py $(VERSION)
