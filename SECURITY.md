# Security

## What this project promises

The box is a wall between AI agents that run without permission prompts and your machine. A security
bug is anything that breaks that wall:

- the box reaching your network, your files or any private address;
- an agent writing the harness, `walls.md`, the desk's book or the vault;
- the Head running anything other than `assign` and `ask`;
- the door letting through a site listed in `door/shut.txt`, or, in allowlist mode, anything not in
  `door/allowlist.txt`;
- a vault value showing up in a log, a report or a page.

## What it does not promise

Agents inside the box are untrusted by design. They can misuse any key you give them, and a page they
read can try to steer them. The Claude token in `.env` is readable from inside the box. Give the box
only keys you are willing to lose, scoped as narrowly as each service allows, and revoke them when
you stop.

## Reporting

Please do not put the details in a public issue. Open an issue that says only that you have a
security report, and the maintainer will reply with a private way to send it.
