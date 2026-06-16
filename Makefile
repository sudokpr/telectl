UV ?= uv
UV_CACHE_DIR ?= .uv-cache
PYTHON ?= python
SERVICE_NAME ?= telegram-control
SERVICE_UNIT ?= $(SERVICE_NAME).service
HTTP_HEALTH_URL ?= http://127.0.0.1:8787/health

.PHONY: help sync run compile test check \
	start stop restart status logs logs-follow \
	service-start service-stop service-restart service-status service-logs service-logs-follow \
	logs-image health

help:
	@printf '%s\n' 'Targets:'
	@printf '  %-22s %s\n' 'sync' 'Install/update dependencies with uv'
	@printf '  %-22s %s\n' 'run' 'Run the bot in the foreground'
	@printf '  %-22s %s\n' 'compile' 'Compile core Python modules'
	@printf '  %-22s %s\n' 'test' 'Run Python tests'
	@printf '  %-22s %s\n' 'check' 'Run sync, compile, and tests'
	@printf '  %-22s %s\n' 'start' 'Alias for service-start'
	@printf '  %-22s %s\n' 'stop' 'Alias for service-stop'
	@printf '  %-22s %s\n' 'restart' 'Alias for service-restart'
	@printf '  %-22s %s\n' 'status' 'Alias for service-status'
	@printf '  %-22s %s\n' 'logs' 'Alias for service-logs'
	@printf '  %-22s %s\n' 'logs-follow' 'Alias for service-logs-follow'
	@printf '  %-22s %s\n' 'service-start' 'Create/start the user systemd service with systemd-run'
	@printf '  %-22s %s\n' 'service-stop' 'Stop the user systemd service'
	@printf '  %-22s %s\n' 'service-restart' 'Restart the user systemd service'
	@printf '  %-22s %s\n' 'service-status' 'Show user systemd service status'
	@printf '  %-22s %s\n' 'service-logs' 'Show recent service journal lines'
	@printf '  %-22s %s\n' 'service-logs-follow' 'Follow service journal logs'
	@printf '  %-22s %s\n' 'logs-image' 'Follow image summary worker log'
	@printf '  %-22s %s\n' 'health' 'Check local HTTP intake health endpoint'

sync:
	$(UV) --cache-dir $(UV_CACHE_DIR) sync

run:
	$(UV) --cache-dir $(UV_CACHE_DIR) run $(PYTHON) bot.py

compile:
	$(UV) --cache-dir $(UV_CACHE_DIR) run $(PYTHON) -m py_compile bot.py image_summary.py

test:
	$(UV) --cache-dir $(UV_CACHE_DIR) run pytest

check: sync compile test

start: service-start

stop: service-stop

restart: service-restart

status: service-status

logs: service-logs

logs-follow: service-logs-follow

service-start:
	@if systemctl --user is-active --quiet $(SERVICE_UNIT); then \
		printf '%s\n' '$(SERVICE_UNIT) is already running'; \
	else \
		systemd-run --user --unit=$(SERVICE_NAME) --working-directory=$(CURDIR) $(UV) --cache-dir $(UV_CACHE_DIR) run $(PYTHON) bot.py; \
	fi

service-stop:
	systemctl --user stop $(SERVICE_UNIT)

service-restart:
	@if systemctl --user list-unit-files $(SERVICE_UNIT) >/dev/null 2>&1 || systemctl --user status $(SERVICE_UNIT) >/dev/null 2>&1; then \
		systemctl --user restart $(SERVICE_UNIT); \
	else \
		$(MAKE) service-start; \
	fi

service-status:
	systemctl --user status $(SERVICE_UNIT) --no-pager

service-logs:
	journalctl --user -u $(SERVICE_UNIT) -n 100 --no-pager

service-logs-follow:
	journalctl --user -u $(SERVICE_UNIT) -f

logs-image:
	tail -f data/image-summary/worker.log

health:
	curl --fail --show-error --silent $(HTTP_HEALTH_URL)
	@printf '\n'
