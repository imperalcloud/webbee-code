# Webbee 🐝 — the coding agent in your terminal

[![PyPI](https://img.shields.io/pypi/v/webbee.svg)](https://pypi.org/project/webbee/)
[![Python](https://img.shields.io/pypi/pyversions/webbee.svg)](https://pypi.org/project/webbee/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-imperal.io-00afd7.svg)](https://docs.imperal.io)

Webbee is the [Imperal Cloud](https://imperal.io) coding agent, in your terminal. It reads, writes, and runs code in your working directory — while the brain runs in the cloud on **ICNLI**, the open protocol behind Webbee. No model keys on your machine. Swap the model underneath and it behaves the same, because the safety was never in the model.

**The model proposes. The kernel decides. The key — delete, drop, wipe — stays with you.**

## Install

```sh
pipx install webbee          # recommended — or:  uv tool install webbee
```

Plain `pip` works too, inside a virtualenv:

```sh
python3 -m venv .venv && . .venv/bin/activate && pip install webbee
```

> On Ubuntu/Debian a *global* `pip install` is blocked by the system (that's
> [PEP 668](https://peps.python.org/pep-0668/), not webbee) — use `pipx` or a
> venv. No Python on the box? `curl -LsSf https://webbee.imperal.io/install.sh | sh`.

## Use

```sh
webbee            # start the agent in the current directory
webbee login      # sign in to your Imperal account (opens the browser)
```

Type in plain English. Webbee reads your files, runs commands, and reaches your connected Imperal apps — mail, notes, tasks, and more — to get the job done. `/help` lists the commands: `/login` `/logout` `/mode` `/cost` `/status` `/clear` `/exit`.

## Modes — you hold the key

Cycle with **Shift + TAB**:

- **default** — Webbee does the small, reversible stuff herself. Anything she can't undo, she stops and asks you first.
- **plan** — read-only. She plans and reads; she touches nothing.
- **autopilot** — she acts without asking. (Spending money always needs a browser approval — no terminal reply can release it.)

## How it works

Your machine runs the hands — read, write, edit, run. The brain runs in the Imperal Cloud and reasons over your files, your history, and your connected apps through ICNLI. The model is a replaceable proposer at the edge; the kernel resolves, grounds, and decides. Webbee reads your facts. She doesn't invent them.

## Links

- **Imperal Cloud** — [imperal.io](https://imperal.io)
- **Docs** — [docs.imperal.io](https://docs.imperal.io)
- **ICNLI** — the open protocol, CC BY-SA 4.0 — [icnli.org](https://icnli.org)
- **More from Imperal** — [github.com/imperalcloud](https://github.com/imperalcloud)

---

MIT © Imperal, Inc.
