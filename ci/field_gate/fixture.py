"""The field-gate workspace: a folder of git repos shaped like the ones the field tests broke on, and the truth about it.

Ignat's wpanel (reports 2026-11-13/14): the root is no repo, nested repos under it, one repo name prefixing another
(`wpanel/` duplicates), a package re-export, Russian docstrings, TS arrow functions inside a component, a route under a
project's own decorator, files with and without a final newline, CRLF, a big file, a secret file and a token in a git
remote. The truth is computed here from bytes and git, once; a scenario compares a product's answer with it and never
recomputes anything a product computes.
"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

# planted fakes: a scenario fails if any of them shows up in a tool's output
FAKE_ENV_TOKEN = "gate_fake_7f3a9c"
FAKE_REMOTE_TOKEN = "ghp_FAKEgate000000000000000000000000000"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Field Gate", "GIT_AUTHOR_EMAIL": "gate@imperal.invalid",
    "GIT_COMMITTER_NAME": "Field Gate", "GIT_COMMITTER_EMAIL": "gate@imperal.invalid",
    "GIT_AUTHOR_DATE": "2026-11-14T12:00:00+00:00", "GIT_COMMITTER_DATE": "2026-11-14T12:00:00+00:00",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


@dataclass(frozen=True)
class Place:
    path: str          # relative to the fixture root
    symbol: str = ""
    text: str = ""


@dataclass(frozen=True)
class Reexport:
    symbol: str
    defined_path: str
    reexport_path: str
    caller_path: str


@dataclass
class Truth:
    lines: dict[str, int] = field(default_factory=dict)          # root-relative path -> lines as an editor counts
    git_files: dict[str, list[str]] = field(default_factory=dict)
    heads: dict[str, str] = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)
    reexport: Reexport | None = None
    ru_doc: Place | None = None
    nested_ts: Place | None = None
    door_route: Place | None = None


@dataclass
class Fixture:
    root: Path
    repos: dict[str, Path]
    truth: Truth


def _lines_of(data: bytes) -> int:
    """Lines as an editor counts them: `wc -l` plus one for a last line with no newline."""
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def _write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
                          env={**os.environ, **_GIT_ENV}).stdout.strip()


def _commit_repo(repo: Path, files: Mapping[str, str | bytes], remote: str = "") -> None:
    for rel, data in files.items():
        _write(repo / rel, data)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "core.autocrlf", "false")
    if remote:
        _git(repo, "remote", "add", "origin", remote)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")


_RU_DOC = "Сверяет права доступа сессии с версией политики и сохраняет сессию."

_APP = {
    "app/__init__.py": "",
    "app/use.py": "from lib_sdk import Emitter\n\n\ndef run():\n    return Emitter().emit(\"ready\")\n",
    "web/Form.tsx": (
        "export function Form() {\n"
        "  const handleSubmit = (event: Event) => {\n"
        "    event.preventDefault();\n"
        "  };\n"
        "  return <form onSubmit={handleSubmit} />;\n"
        "}\n"
    ),
    "mid.py": "".join(f"value_{i} = {i}\n" for i in range(1, 601)),
    "no_nl.py": "a = 1\nb = 2",
    "crlf.py": b"a = 1\r\nb = 2\r\n",
}

_GATEWAY = {
    "gateway/__init__.py": "",
    "gateway/authz.py": (
        "def reconcile_authz(session, policy_version):\n"
        f"    \"\"\"{_RU_DOC}\"\"\"\n"
        "    if session.get(\"policy_version\") == policy_version:\n"
        "        session[\"authz\"] = True\n"
        "    return session\n"
    ),
    "gateway/doors.py": (
        "def door(path):\n"
        "    def wrap(fn):\n"
        "        fn.route = path\n"
        "        return fn\n"
        "    return wrap\n"
    ),
    "gateway/routes.py": "from gateway.doors import door\n\n\n@door(\"/v1/x\")\nasync def get_x():\n    return {\"x\": 1}\n",
    "big.py": "".join(f"row_{i} = {i}\n" for i in range(1, 6002)),
    ".gitignore": ".env\n",
}

_LIB = {
    "lib_sdk/__init__.py": "from .emit import Emitter\n\n__all__ = [\"Emitter\"]\n",
    "lib_sdk/emit.py": "class Emitter:\n    def emit(self, event):\n        return event\n",
}


def build_fixture(dest: Path) -> Fixture:
    root = Path(dest)
    root.mkdir(parents=True, exist_ok=False)
    _write(root / "README.txt", "A folder of repositories; the folder itself is no repository.\n")
    repos = {"app": root / "core" / "app", "app-gateway": root / "core" / "app-gateway", "lib-sdk": root / "lib-sdk"}
    _commit_repo(repos["app"], _APP)
    _commit_repo(repos["app-gateway"], _GATEWAY,
                 remote=f"https://gate:{FAKE_REMOTE_TOKEN}@example.invalid/app-gateway.git")
    _write(repos["app-gateway"] / ".env", f"TOKEN={FAKE_ENV_TOKEN}\n")     # on disk, ignored by git
    _commit_repo(repos["lib-sdk"], _LIB)

    truth = Truth(secrets=[FAKE_ENV_TOKEN, FAKE_REMOTE_TOKEN])
    for name, repo in repos.items():
        files = sorted(_git(repo, "ls-files").split())
        truth.git_files[name] = files
        truth.heads[name] = _git(repo, "rev-parse", "HEAD")
        for rel in files:
            full = repo / rel
            truth.lines[full.relative_to(root).as_posix()] = _lines_of(full.read_bytes())
    truth.reexport = Reexport("Emitter", "lib-sdk/lib_sdk/emit.py", "lib-sdk/lib_sdk/__init__.py", "core/app/app/use.py")
    truth.ru_doc = Place("core/app-gateway/gateway/authz.py", "reconcile_authz", _RU_DOC)
    truth.nested_ts = Place("core/app/web/Form.tsx", "handleSubmit")
    truth.door_route = Place("core/app-gateway/gateway/routes.py", "get_x", "/v1/x")
    return Fixture(root=root, repos=repos, truth=truth)
