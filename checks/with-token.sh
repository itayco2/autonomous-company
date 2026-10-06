#!/bin/sh
# The checks that need the Claude token, in one run. Run it yourself: Docker reads .env here, and a
# Claude session is not allowed to read .env files. Everything printed also goes to
# log/checks/with-token.txt, which holds no secret. With --calibrate, one calibration cycle runs
# afterwards, and only if every check before it passed.
cd "$(dirname "$0")/.." || exit 1
mkdir -p log/checks
# The checks get an empty book and vault of their own (checks/boxrun.py does the same for the Python
# checks): a broken read-only mount can then never write into the owner's, and no check sees the owner's values.
# The door's mode as compose resolves it: DOOR_MODE from the shell, then from .env, else open.
door_mode=${DOOR_MODE:-$(sed -n 's/^DOOR_MODE=[\"'"'"']*\([a-z]*\).*/\1/p' .env 2>/dev/null | tail -1)}; door_mode=${door_mode:-open}
rm -rf log/checks/desk-book log/checks/desk-vault
mkdir -p log/checks/desk-book && mkdir -p -m 700 log/checks/desk-vault
{
  failed=0
  echo "=== walls"
  # `run box` starts only the door, but the walls probe the office and the desk as well. They are the
  # owner's real ones and stay up afterwards, so they start on the owner's own book. The desk probe passes when
  # the box cannot reach the desk, which means something only while the desk answers on the Mac.
  docker compose up -d --no-deps office desk >/dev/null 2>&1
  desk_up=no
  for i in 1 2 3 4 5 6 7 8 9 10; do
    curl -sf -o /dev/null -H 'Origin: http://127.0.0.1:8771' http://127.0.0.1:8772/health && { desk_up=yes; break; }
    sleep 1
  done
  if [ "$desk_up" = no ]; then
    echo "CHECK 2 FAIL: the front desk does not answer on the Mac, so the probe that the box cannot reach it would prove nothing"
    failed=1
  elif DESK_BOOK=./log/checks/desk-book DESK_VAULT=./log/checks/desk-vault WORKSPACE_VOLUME=autonomous-company_checks \
      docker compose run --rm -T -e CLAUDE_CODE_OAUTH_TOKEN= box \
      python3 - "$door_mode" < checks/walls.py 2>/dev/null | tail -1 | grep -q "RESULT PASS"; then
    echo "CHECK 2 PASS"
  else
    echo "CHECK 2 FAIL"; failed=1
  fi
  for check in roles headless permissions delegate together search; do
    echo "=== $check"
    python3 "checks/$check.py" || failed=1
  done
  echo "=== halt --real"
  DESK_BOOK=./log/checks/desk-book DESK_VAULT=./log/checks/desk-vault python3 checks/halt.py --real || failed=1
  if [ "$1" = "--calibrate" ]; then
    if [ "$failed" = 0 ]; then
      echo "=== calibrate, one cycle (up to 20 minutes; keep Claude idle meanwhile)"
      python3 checks/calibrate.py --cycles 1
    else
      echo "=== calibrate skipped: a check above failed"
    fi
  fi
  echo "=== done $(date '+%H:%M')"
} 2>&1 | tee log/checks/with-token.txt
