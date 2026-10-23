# Webbee 🐝 — The Open-Source ICNLI Coding Agent

[![PyPI](https://img.shields.io/pypi/v/webbee)](https://pypi.org/project/webbee/)
[![Python](https://img.shields.io/badge/python-3.7%20%7C%203.8%20%7C%203.9%20%7C%203.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-3776ab?logo=python&logoColor=white)](https://pypi.org/project/webbee/))
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-imperal.io-00afd7)](https://docs.imperal.io)

**Python 3.7 – 3.14** · `pip install webbee` works on every one of them.

**Webbee** is the free, open-source terminal coding agent of [Imperal Cloud](https://imperal.io) (the ICNLI AI Cloud OS). It acts directly in your local terminal and workspace: reading, writing, editing, refactoring, and verifying code with deep code intelligence, native multimodal vision, and live cross-platform system capabilities.

Webbee is powered by **ICNLI** (*Infrastructure Contextual Natural Language Interface* — [icnli.org](https://icnli.org)), the open protocol that maps natural-language intent to context-aware infrastructure actions.

> **Open-Source Guarantee (GPLv3)**: Webbee belongs to the community. No locked silos, no hidden telemetry, no arbitrary execution barriers. The model proposes; the kernel evaluates; **you hold the key**.

---

## 🚀 Key Features

* 🧠 **Real Agentic AI Brain, Not a Chatbot**: Reasons across your entire workspace, plans multi-step tasks, reads project architecture, executes terminal commands, and edits code safely.
* 👁️ **Native Multimodal Vision (`view_image`)**: Direct image inspection (UI mocks, diagrams, screenshots, graphs) for vision-capable models, with automatic graceful fallback to OCR and document extraction via File Reader.
* 🌐 **Integrated Web Search (`web_search` & `read_url`)**: Perform live web research and retrieve documentation directly in your terminal sessions via Imperal's system web-search extension — zero local API keys required.
* 🛡️ **Ironclad Safety & Reversibility**: Built-in Shadow Git time-machine snapshots every workspace modification (`checkpoint`, `diff`, `rollback`). Revert any step instantly.
* 🔌 **Platform & Extension Arsenal**: Seamless access to connected cloud extensions (Mail, Vikunja Tasks, Notes, Google Analytics, GitHub, Proxmox, Stripe, SSH Connections, and more).
* ⚡ **Lightning Fast & Memory-Efficient**: Instant startup with granular lazy imports, configurable RAM limits (256MB to 8GB) with LRU eviction, and fail-soft permission handling.
* 🖥️ **First-Class Cross-Platform TUI**: Beautiful, responsive terminal interface compatible with macOS (Terminal.app, iTerm2), Linux (Alacritty, Kitty, WezTerm), and Windows Terminal. Normalized 2-cell icon widths for pixel-perfect alignment.

---

## 📦 Installation

```sh
pipx install webbee
# or:  uv tool install webbee
# or:  pip install webbee        (inside a virtual environment)
```

No Python tools at hand? One line installs [uv](https://docs.astral.sh/uv/) (it brings its own Python) and webbee:

```sh
curl -LsSf https://raw.githubusercontent.com/imperalcloud/webbee-code/main/install.sh | sh
```

Works with **any Python from 3.7**:

- **Python 3.10 or newer** — webbee runs directly on it.
- **Python 3.7 – 3.9** (macOS ships 3.9, Ubuntu 20.04 has 3.8) — the install is light, and the first
  `webbee` command sets up the same release on its own Python 3.12, once (about 30–45 s, needs internet;
  it fetches uv to do it). Every start after that is immediate, and it is the full webbee — code index
  and search by meaning included. It lives in `~/.local/share/webbee/runtime` and changes nothing else on
  your system.

<details>
<summary>Install problems</summary>

| You see | Why | Do |
|---------|-----|----|
| `Cannot install webbee==… because these package versions have conflicting dependencies` / `ResolutionImpossible` | a webbee release older than 0.5.6 on Python below 3.10 | `pipx install "webbee>=0.5.6"` (or `pip install -U webbee`) |
| `error: externally-managed-environment` | `pip install` into the system Python is blocked on Homebrew, Debian 12+, Ubuntu 23.04+ | `pipx install webbee` or `uv tool install webbee` |
| `webbee: command not found` after install | the tool folder is not on `PATH` | `pipx ensurepath` (or `uv tool update-shell`), then open a new terminal |
| `Could not set up webbee on Python 3.12` on the first start | Python 3.7–3.9 and no internet (or a proxy blocks github.com / pypi.org) | allow those hosts and start `webbee` again, or use the one-line installer above |
| `No matching distribution found for webbee` | Python 3.6 or older | the one-line installer above — it brings its own Python |

</details>

---

## 💡 Quick Start

```sh
# Start Webbee in your project workspace
webbee

# Sign in to link your Imperal Cloud account (RFC 8628 device flow)
webbee login
```

Inside the interactive terminal:
* Speak naturally in English, Ukrainian, or any language you prefer.
* Type `/help` to see all available slash-commands (`/login`, `/mode`, `/status`, `/cost`, `/clear`, `/exit`).
* Use **Shift + TAB** to cycle execution modes:
  - **default**: Asks confirmation for major changes or destructive operations.
  - **plan**: Formulates an exhaustive execution strategy before touching any code.
  - **autopilot**: Autonomous mode for rapid prototyping and uninterrupted development runs.

---

## 🛠️ Built-in Terminal Toolset

| Icon | Tool | Description |
|:---:|:---|:---|
| 📖 | `read_file` | Read text and source files byte-exact, with offset/limit paging |
| ✏️ | `write_file` | Create or overwrite files atomically with snapshot tracking |
| 🔧 | `edit_file` / `multi_edit` | Precise find-and-replace edits across one or multiple files |
| ⚡ | `shell` | Execute shell/terminal commands across Windows, macOS and Linux with streaming |
| 🔎 | `grep` / `glob` | Fast regex code search and workspace file pattern matching |
| 🌐 | `web_search` / `read_url` | Search the web and read web documentation in clean Markdown |
| 🖼️ | `view_image` | Inspect images with native multimodal vision + OCR fallback |
| 🗂️ | `diff` / `rollback` | Inspect recent changes and roll back to previous checkpoints |

---

## 📜 License

Webbee is free and open-source software licensed under the **GNU General Public License v3.0** (`GPL-3.0-or-later`). See the [LICENSE](LICENSE) file for details.

Developed with 🐝 by the [Imperal Cloud](https://imperal.io) team.
