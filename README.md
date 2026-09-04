# Webbee 🐝 — The Open-Source ICNLI Coding Agent

[![PyPI](https://img.shields.io/pypi/v/webbee)](https://pypi.org/project/webbee/)
[![Python](https://img.shields.io/pypi/pyversions/webbee)](https://pypi.org/project/webbee/)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-imperal.io-00afd7)](https://docs.imperal.io)

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

Install easily using **pipx** (recommended) or **uv**:

```sh
pipx install webbee
# or with uv:
uv tool install webbee
```

Or install in a Python virtual environment:

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install webbee
```

### 🐍 Supported Python Versions

Webbee is officially tested and supported on:
* **Python 3.9**
* **Python 3.10**
* **Python 3.11**
* **Python 3.12**
* **Python 3.13**
* **Python 3.14**

> **Compatibility**: Requires Python `>=3.9`. Works natively across **macOS** (Apple Silicon M1–M4 & Intel), **Linux** (x86_64, aarch64), and **Windows** (PowerShell, Windows Terminal).

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
