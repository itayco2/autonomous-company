"""Check 5: web search works in a headless run through the door, in either of its modes.

Run on the Mac:  python3 checks/search.py
Also records what happens to WebFetch, which opens pages from inside the box and so meets the door.
"""
import json
import sys
import time

from boxrun import ROOT, box, result, tool_calls

WORKER = ["--model", "haiku", "--tools", "WebSearch,WebFetch", "--permission-mode", "bypassPermissions",
          "--setting-sources", "", "--output-format", "stream-json", "--verbose"]
since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

searched = box("claude", "-p", "Use the WebSearch tool once to search for: Claude Code changelog. "
               "Reply with the URL of the first result and nothing else.", *WORKER, name="search")
fetched = box("claude", "-p", "Use the WebFetch tool once to fetch https://example.com and reply with the "
              "page title, or with the exact error if the fetch fails.", *WORKER, name="fetch")

for label, events in (("search", searched), ("fetch", fetched)):
    final = result(events)
    print(f"{label}: result={str(final.get('result'))[:200]!r} server_tool_use={final.get('usage', {}).get('server_tool_use')}")
    for name, arguments, is_error, output in tool_calls(events):
        print(f"  {name} error={is_error} -> {output[:160]!r}")

door = [json.loads(l) for l in (ROOT / "log" / "door.jsonl").read_text().splitlines()]
door = [r for r in door if r["ts"] >= since and r["event"] in ("allow", "refuse")]
print("door since start:", sorted({(r["event"], r["host"]) for r in door}))

answer = str(result(searched).get("result", ""))
# The WebSearch tool runs its search in a separate model call, so the session's own server_tool_use
# stays at 0 (measured 2026-09-30). The evidence is the tool's own result: real links came back.
searched_ok = any(name == "WebSearch" and is_error is False and "Links:" in output
                  for name, _, is_error, output in tool_calls(searched))
# The door logs its mode when it opens. Only an allowlist door must keep the box to the model alone;
# an open door lets WebFetch reach the page, as it should.
modes = [r.get("mode") for r in (json.loads(l) for l in (ROOT / "log" / "door.jsonl").read_text().splitlines()) if r.get("event") == "open"]
door_mode = modes[-1] if modes else "allowlist"
only_allowlisted = door_mode == "open" or all(r["host"] == "api.anthropic.com" for r in door if r["event"] == "allow")
ok = searched_ok and "http" in answer and only_allowlisted and not result(searched).get("is_error")
print(f"CHECK 5 {'PASS' if ok else 'FAIL'}: WebSearch returned links: {searched_ok}, an answer with a URL: "
      f"{'http' in answer}, door mode: {door_mode}, box kept to allowed hosts: {only_allowlisted}")
sys.exit(0 if ok else 1)
