# Contributing

Thanks for looking. A few rules keep this project trustworthy.

## Before you change anything

- `make test` runs the fast tier: standard library only, no Docker and no model, a few seconds. It
  must pass before and after your change. CI runs it on every push and pull request.
- A change to the box, the door, the desk or `walls.md` also needs `make checks`, which builds the
  containers and probes the walls. If it touches anything that calls the model, run
  `make checks-token` in your own terminal as well.

## Every defect gets a row and a test

`PREFLIGHT.md` lists every defect found so far, with the number that exposed it and the number after
the fix. When you fix a bug, add a row there and a test that fails without your fix. A finding that
came from reading the code rather than running it says so in its row.

## What must stay true

- The Head can run only `assign` and `ask` (`box/bin/only-assign.py`).
- Nothing inside the box can write the harness, `walls.md`, the desk's book or the vault. They are
  mounted read-only.
- The box has no route out except the door, and the door never connects to a private address.
- Vault values never reach a log, a report or a page. `box/bin/cycle.py` redacts them from the stream.
- Acting in the world waits for the owner's yes at the front desk, unless the owner changes
  `walls.md` themselves.

A pull request that weakens one of these says why in its description.

## Style

- Outside the box, Python 3 and its standard library only.
- Comments say why, not what.
- One change per pull request, kept small.
