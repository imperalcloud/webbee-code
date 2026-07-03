# Webbee 🐝 — coding agent in your terminal

Webbee is the Imperal Cloud coding agent. It reads, writes, and runs code in
your current directory, while the brain runs safely in the Imperal Cloud —
no model keys, no vendor lock-in.

## Install

```sh
# one line, no Python needed:
curl -LsSf https://webbee.imperal.io/install.sh | sh

# or, if you have pipx / uv:
pipx install webbee
uv tool install webbee
```

## Use

```sh
webbee            # start the coding REPL in the current directory
webbee login      # log in to your Imperal account
```

Inside the REPL: `/help` lists commands (`/login`, `/mode`, `/cost`,
`/status`, `/clear`, `/exit`). Works on macOS, Linux, and Windows.

## Update

```sh
pipx upgrade webbee    # or: uv tool upgrade webbee
```

Learn more at [imperal.io](https://imperal.io) · [docs.imperal.io](https://docs.imperal.io).
