#!/usr/bin/env python3
"""Records a run: a frame of the live office every 20 seconds and the
company's own view every 5 minutes, into media/ (git-ignored). Run `make capture` beside
`make start`; it stops by itself once the heartbeat has stopped. `make timelapse` turns the office
frames into a video.

Headless Chrome with a mock keychain and a throwaway profile, never the owner's real one.
"""
import json
import os
import pathlib
import subprocess
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent
MEDIA = ROOT / "media"
EVENTS = ROOT / "log" / "heartbeat.jsonl"
CHROME = os.environ.get("CHROME_BIN", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
PROFILE = tempfile.mkdtemp(prefix="autonomous-company-capture-")
EVERY = {"office": 20, "view": 300}
PAGES = {"office": ("http://127.0.0.1:8771", 1440, 1300), "view": ("http://127.0.0.1:8770", 1440, 900)}


def shot(name):
    url, width, height = PAGES[name]
    folder = MEDIA / name
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / (time.strftime("%Y%m%d-%H%M%S") + ".png")
    try:
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--use-mock-keychain",
                        "--no-first-run", f"--user-data-dir={PROFILE}", f"--window-size={width},{height}",
                        "--virtual-time-budget=5000", f"--screenshot={out}", url],
                       capture_output=True, timeout=90)
    except subprocess.TimeoutExpired:
        return None
    return out if out.exists() else None


def heartbeat_stopped():
    try:
        last = EVENTS.read_text().splitlines()[-1]
        return json.loads(last).get("event") == "stopped"
    except (OSError, IndexError, ValueError):
        return False


def main():
    due = {name: 0.0 for name in PAGES}
    print(f"capturing into {MEDIA} until the heartbeat stops", flush=True)
    while True:
        stopping = heartbeat_stopped()
        for name, seconds in EVERY.items():
            if stopping or time.time() >= due[name]:
                saved = shot(name)
                due[name] = time.time() + seconds
                if saved:
                    print(f"{time.strftime('%H:%M:%S')} {name}: {saved.name}", flush=True)
        if stopping:
            print("the heartbeat stopped; so does the capture", flush=True)
            return
        time.sleep(2)


if __name__ == "__main__":
    main()
