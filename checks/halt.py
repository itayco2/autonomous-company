"""Check 8: `docker pause` freezes a running cycle, and killing its container ends it.

Run on the Mac:  python3 checks/halt.py [--real]
Without --real the cycle is a stand-in that bumps a counter five times a second. With --real it is
a Claude session that bumps the same counter through its Bash tool, one call at a time, so pause
and kill are proven on a live model session and not only on a shell loop.

A cycle is its own container, so ending one is `docker kill`: killing a `docker exec` client
leaves the process running inside the container. Whether the heartbeat turns the stop file into
that kill is the heartbeat's own check; this one proves the kill itself.
"""
import os
import pathlib
import subprocess
import sys
import time

os.environ.setdefault("WORKSPACE_VOLUME", "autonomous-company_checks")
ROOT = pathlib.Path(__file__).resolve().parent.parent
NAME = "ac-check8"
COUNTER = "/workspace/checks/c8.count"
STAND_IN = f"mkdir -p /workspace/checks; n=0; while true; do n=$((n+1)); echo $n > {COUNTER}; sleep 0.2; done"
REAL = ["bash", "-c", "mkdir -p /workspace/checks && exec claude -p \"$0\" --model haiku "
        "--permission-mode bypassPermissions --setting-sources ''",
        f"Using the Bash tool, run `echo N > {COUNTER}` for N = 1, 2, 3 and so on up to 300, "
        "one separate Bash call per number. Do not combine calls."]


def sh(*args):
    done = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    return done.returncode, done.stdout.strip()


def count():
    """The counter, or None while the file does not exist. Read from a separate network-less
    container, because a paused one cannot be exec'd into."""
    code, out = sh("docker", "run", "--rm", "--network", "none", "-v", f"{os.environ['WORKSPACE_VOLUME']}:/w:ro",
                   "autonomous-company-box", "sh", "-c", f"cat /w/{COUNTER.removeprefix('/workspace/')}")
    return out if code == 0 and out else None


def running():
    return sh("docker", "ps", "-q", "--filter", f"name=^{NAME}$")[1] != ""


def main():
    real = "--real" in sys.argv[1:]
    window = 8 if real else 1.5
    sh("docker", "rm", "-f", NAME)
    sh("docker", "run", "--rm", "--network", "none", "-v", f"{os.environ['WORKSPACE_VOLUME']}:/w",
       "autonomous-company-box", "rm", "-f", f"/w/{COUNTER.removeprefix('/workspace/')}")
    sh("docker", "compose", "run", "-d", "--rm", "--name", NAME, "box", *(REAL if real else ["bash", "-c", STAND_IN]))
    deadline = time.monotonic() + 120
    while count() is None and running() and time.monotonic() < deadline:
        time.sleep(1)
    results = []

    a = count(); time.sleep(window); b = count()
    results.append(("running cycle makes progress", b is not None and a != b, f"{a!r} -> {b!r}"))
    if not running():
        results.append(("docker pause freezes it", False, "the cycle had already ended, so it could not be paused"))
    else:
        code, _ = sh("docker", "pause", NAME)
        state = sh("docker", "inspect", "-f", "{{.State.Status}}", NAME)[1]
        a = count(); time.sleep(2 * window); b = count()
        results.append(("docker pause freezes it", code == 0 and state == "paused" and a is not None and a == b,
                        f"state={state}; {a!r} -> {b!r} over {2 * window:g} s"))
        sh("docker", "unpause", NAME)
        a = count(); time.sleep(window); b = count()
        results.append(("docker unpause resumes it", b is not None and a != b, f"{a!r} -> {b!r}"))

    started = time.monotonic()
    sh("docker", "kill", NAME)
    while running() and time.monotonic() - started < 30:
        time.sleep(0.5)
    gone_after = time.monotonic() - started
    last = count(); time.sleep(window)
    still = count() == last
    results.append(("docker kill ends it", not running() and still,
                    f"container gone {gone_after:.1f} s after the kill; counter still afterwards: {still}"))

    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    ok = all(r[1] for r in results)
    print(f"CHECK 8{' (live session)' if real else ''} {'PASS' if ok else 'FAIL'}: "
          f"{sum(r[1] for r in results)}/{len(results)} (the stop file itself is checked with the heartbeat)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
