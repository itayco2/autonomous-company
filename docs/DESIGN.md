# Design

One AI, the Head, runs a company in cycles inside a sealed Docker box. It decides what the company
is, which roles exist and what each one does, and it does nothing else: it writes task files and
queues them with `assign`. Worker agents run the tasks and leave reports. The person who started it
watches, answers requests at a front desk, and can stop it at any moment.

## The parts

| Part | File | What it does |
|---|---|---|
| Heartbeat | `heartbeat.py` | Runs on your machine. Starts one cycle at a time, watches the weekly usage share it may spend, and stops cleanly when you stop it. |
| Cycle | `box/bin/cycle.py` | Runs inside the box. Starts the Head's session, then the workers it queued, and streams everything to the outside log. |
| Company rules | `box/bin/company.py` | Shared rules: where reports go, what a request looks like, how many agents run at once. |
| assign | `box/bin/assign.py` | The Head's one command for work: queues a task for a role. |
| ask | `box/bin/ask.py` | The Head's one command for the owner: puts a request on the front desk. |
| The Head's hook | `box/bin/only-assign.py` | Refuses every command the Head runs except `assign` and `ask`. |
| Door | `door/proxy.py` | The only way out of the box. In `open` mode (the default) it reaches the public internet except `door/shut.txt`; in `allowlist` mode only `door/allowlist.txt`. Logs every decision outside the box. |
| Front desk | `desk/server.py` | The owner answers requests: provide a key, say no, or take something back. Keys go to a vault the box can read and never write. |
| Office | `office/server.py`, `office/office.html` | A live pixel office: every agent at a desk with its current task. Read-only. The Head styles it through `office/look-format.md`. |
| Window | `window/serve.py` | Serves what the company builds in `view/`, read-only, on your machine only. |
| Checks | `checks/walls.py`, `checks/permissions.py` and the rest of `checks/` | Container-level checks run before a real start: the door, the hook, the vault, the roles. |

## What the box cannot do

- Reach your network: the box's network has no route out; everything goes through the door.
- Rewrite its own harness: `box/bin` and `walls.md` are mounted read-only.
- Create an account, inbox or phone number: it asks at the front desk, and only you can provide.
- Spend money: there is no budget unless you write one into `walls.md`, and a purchase is a request
  you answer.
- Act in the world without your yes: it can read and research the whole public internet, but
  `walls.md` makes it ask at the front desk before it publishes, sells, posts or signs up anywhere,
  and every account it could use is one you provided.

## What it can do

Workers run as Claude Code with every tool and without permission prompts inside the box. That is
what lets them build real things, and it is why the box is sealed: treat everything inside it as
untrusted, and give it only keys you are willing to lose.

## Cycles

A cycle starts the Head with no memory except the workspace. It reads its own files, decides, queues
work and ends; then the workers run, up to sixteen at once. Reports land in `company/reports/` with a
first line that says where each task stands. A stop at any moment is normal: a task cut off before
it started is not run later, and the Head decides again next cycle.
