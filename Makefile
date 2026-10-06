# autonomous-company runbook. `make` on its own lists the targets.
.DEFAULT_GOAL := help
# Targets are commands, not files: without this, `make checks` does nothing, because a checks/ folder exists.
.PHONY: help start stop status view office test checks checks-token capture timelapse

# The door's mode as compose resolves it: DOOR_MODE from the shell, then from .env, else open.
DOOR_MODE ?= $(or $(shell sed -n 's/^DOOR_MODE=[\"'"'"']*\([a-z]*\).*/\1/p' .env 2>/dev/null | tail -1),open)
# The checks get an empty book and vault of their own (checks/boxrun.py does the same for the Python
# checks): a broken read-only mount can then never write into the owner's, and no check sees the owner's values.
CHECK_DESK := DESK_BOOK=./log/checks/desk-book DESK_VAULT=./log/checks/desk-vault
# The walls' desk probe passes when the box cannot reach the desk, which means something only while the
# desk is up. So first it must answer on the Mac; a container just started takes a moment to listen.
DESK_ANSWERS := for i in 1 2 3 4 5 6 7 8 9 10; do curl -sf -o /dev/null -H 'Origin: http://127.0.0.1:8771' http://127.0.0.1:8772/health && exit 0; sleep 1; done; exit 1

help: ## list the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-13s %s\n", $$1, $$2}'

# The company's share of the weekly allowance, in percentage points (configure as needed).
SHARE ?= 20

# macOS keeps the machine awake while the company runs and opens pages with `open`. Linux has no
# caffeinate, and opens pages with xdg-open.
ifeq ($(shell uname -s),Darwin)
KEEP_AWAKE := caffeinate -is
OPEN := open
else
KEEP_AWAKE :=
OPEN := xdg-open
endif

start: ## run the company, cycle after cycle, until `make stop`; keeps the machine awake meanwhile (macOS caffeinate)
	$(KEEP_AWAKE) python3 heartbeat.py --share $(SHARE)

capture: ## record the office and the company's view into media/ while it runs (only when wanted)
	python3 capture.py

timelapse: ## turn the recorded office frames into media/office-timelapse.mp4
	@FFMPEG=$${FFMPEG:-ffmpeg}; \
	$$FFMPEG -y -loglevel error -framerate 12 -pattern_type glob -i 'media/office/*.png' \
	  -vf "scale=1440:-2,format=yuv420p" -c:v libx264 -crf 20 media/office-timelapse.mp4 && \
	echo "media/office-timelapse.mp4: $$(ls media/office/*.png | wc -l | tr -d ' ') frames at 12 per second"

stop: ## stop now: the cycle in progress ends, and the next start resumes from the company's files
	@touch STOP && echo "stopping: the heartbeat ends within a few seconds"
	@sleep 10; if [ -f STOP ]; then \
	  echo "no heartbeat picked up the stop; ending any running cycle directly"; \
	  docker ps -q --filter label=com.docker.compose.project=autonomous-company --filter label=com.docker.compose.service=box | xargs -r docker kill >/dev/null; \
	  rm -f STOP; fi; echo "stopped"

status: ## running or not, the last cycles, and the company's share of this week
	@python3 heartbeat.py --status

view: ## open what the company shows, http://127.0.0.1:8770
	@$(OPEN) http://127.0.0.1:8770

office: ## open the live office: who is doing what, and the front desk for your answers, http://127.0.0.1:8771
	@docker compose up -d --no-deps office desk >/dev/null 2>&1 || echo "the office or the desk did not start: is another project using ports 8771 and 8772?"; $(OPEN) http://127.0.0.1:8771

test: ## the fast tier: pure functions, standard library only, no Docker and no model
	python3 -m unittest discover -s tests

# `run box` starts only the door, but the walls probe the office and the desk as well, so both are started
# first. They are the owner's real office and desk and stay up afterwards, so they never get CHECK_DESK.
checks: ## the checks that need no token: the walls, the model rules, pause and kill
	@rm -rf log/checks/desk-book log/checks/desk-vault && mkdir -p log/checks/desk-book && mkdir -p -m 700 log/checks/desk-vault
	@if ! docker compose up -d --no-deps office desk >/dev/null 2>&1; then \
	  echo "RESULT FAIL: this project's office and desk did not start (is another project using ports 8771 and 8772?)"; \
	elif ( $(DESK_ANSWERS) ); then \
	  $(CHECK_DESK) WORKSPACE_VOLUME=autonomous-company_checks docker compose run --rm -T -e CLAUDE_CODE_OAUTH_TOKEN= box python3 - "$(DOOR_MODE)" < checks/walls.py | tail -1; \
	else echo "RESULT FAIL: the front desk does not answer on the Mac, so the probe that the box cannot reach it would prove nothing"; fi
	$(CHECK_DESK) python3 checks/roles.py | tail -1
	$(CHECK_DESK) python3 checks/halt.py | tail -1

checks-token: ## every check, including the ones that call the model; run it in your own terminal
	sh checks/with-token.sh
