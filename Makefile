UV ?= uv
UV_CACHE_DIR ?= .uv-cache
PYTHON ?= python
SERVICE_NAME ?= telegram-control
SERVICE_UNIT ?= $(SERVICE_NAME).service
SERVICE_FILE ?= $(CURDIR)/systemd/$(SERVICE_UNIT)
HTTP_HEALTH_URL ?= http://127.0.0.1:8787/health

.PHONY: help sync run compile test check \
	start stop restart status logs logs-follow \
	service-start service-stop service-restart service-status service-logs service-logs-follow \
	logs-image health \
	backup-run backup-timers backup-status backup-logs backup-logs-follow

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
	@printf '  %-22s %s\n' 'service-start' 'Enable/start the user systemd service'
	@printf '  %-22s %s\n' 'service-stop' 'Stop the user systemd service'
	@printf '  %-22s %s\n' 'service-restart' 'Restart the user systemd service'
	@printf '  %-22s %s\n' 'service-status' 'Show user systemd service status'
	@printf '  %-22s %s\n' 'service-logs' 'Show recent service journal lines'
	@printf '  %-22s %s\n' 'service-logs-follow' 'Follow service journal logs'
	@printf '  %-22s %s\n' 'logs-image' 'Follow image summary worker log'
	@printf '  %-22s %s\n' 'health' 'Check local HTTP intake health endpoint'
	@printf '  %-22s %s\n' 'backup-run' 'Run the DietPi backup now'
	@printf '  %-22s %s\n' 'backup-timers' 'Show the DietPi backup timer schedule'
	@printf '  %-22s %s\n' 'backup-status' 'Show the DietPi backup service/timer status'
	@printf '  %-22s %s\n' 'backup-logs' 'Show recent DietPi backup logs'
	@printf '  %-22s %s\n' 'backup-logs-follow' 'Follow DietPi backup logs'

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
	systemctl --user enable --now $(SERVICE_FILE)

service-stop:
	systemctl --user stop $(SERVICE_UNIT)

service-restart:
	systemctl --user enable $(SERVICE_FILE)
	systemctl --user restart $(SERVICE_UNIT)

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

backup-run:
	systemctl --user start telegram-control-backup.service

backup-timers:
	systemctl --user list-timers telegram-control-backup.timer --no-pager

backup-status:
	systemctl --user status telegram-control-backup.timer telegram-control-backup.service --no-pager

backup-logs:
	journalctl --user -u telegram-control-backup.service -n 100 --no-pager

backup-logs-follow:
	journalctl --user -u telegram-control-backup.service -f
