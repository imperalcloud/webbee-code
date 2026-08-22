# Copyright (c) 2026 Imperal, Inc.
# Licensed under the AGPL-3.0 License.
"""The bridge: a LOCAL document/image path -> the platform's File Reader.

`read_file` used to open every path as UTF-8 and die on the first .docx or
screenshot. This module is the missing leg: it hands the bytes to the SYSTEM
file-reader extension (the same door the panel dropzone, Telegram and
clipboard paste already use) and hands the brain back a compact text window.

Three invariants this file keeps:

* **No parsers here.** Extraction belongs to the engine behind file-reader.
  The client never sniffs a PDF, never shells out to a converter, and never
  talks to the engine directly -- only through the extension.
* **Bytes never enter the model's context.** They travel base64 inside the
  tool call to the extension; the BRAIN receives text + a ``file_id`` handle
  and pages through it with offset/limit. A 300-page PDF costs one window.
* **Honest degradation.** Offline, no plan, engine warming up, oversized
  file: each is a plain sentence the agent can act on -- never a traceback,
  never a silently empty read.
"""
from __future__ import annotations

import asyncio
import base64
import os

from webbee.native_files import classify, guess_mime, human_size

# What one read_file call pulls back when the caller gave no window. Deliberately
# modest: the point of this path is to keep big documents CHEAP. The agent pages
# on with offset/limit exactly like it does for source files.
DEFAULT_WINDOW_CHARS = 12_000

# Upload ceiling. The extension itself allows 100 MiB, but a terminal read is
# interactive: past this we say so instead of stalling a turn on a huge upload.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# The engine extracts asynchronously; a fresh upload reports status="preparing"
# for a moment. Poll briefly rather than making the agent retry blind.
_POLL_DELAYS = (0.6, 1.0, 1.6, 2.4, 3.4, 4.0, 5.0)


def extract_file_id(resp) -> str:
    """Pull `file_id` from a file-reader `receive_files` response.

    Canon: it lives at ``data.items[].file_id`` (== ``.id``), NOT
    ``data.received``. Handles dict + object shapes; "" if absent.
    """
    try:
        data = resp.get("data") if isinstance(resp, dict) else getattr(resp, "data", None)
        items = data.get("items") if isinstance(data, dict) else getattr(data, "items", None)
        if items:
            it = items[0]
            if isinstance(it, dict):
                return str(it.get("file_id") or it.get("id") or "")
            return str(getattr(it, "file_id", "") or getattr(it, "id", "") or "")
    except Exception:
        pass
    return ""


def _first_item(resp) -> dict:
    """First result dict from a read_files/file_overview response, or {}."""
    try:
        data = resp.get("data") if isinstance(resp, dict) else getattr(resp, "data", None)
        if isinstance(data, list):
            return data[0] if data and isinstance(data[0], dict) else {}
        if isinstance(data, dict):
            for key in ("results", "items", "files"):
                seq = data.get(key)
                if isinstance(seq, list) and seq and isinstance(seq[0], dict):
                    return seq[0]
            return data
    except Exception:
        pass
    return {}


def _status_of(item: dict) -> str:
    """ok | preparing | error | expired, from the field OR from the placeholder.

    Newer file-reader sends `status` on every text window; older builds only
    hinted at it in prose. Reading both means this client is correct against
    whichever version happens to be deployed.
    """
    st = str(item.get("status") or item.get("read_status") or "").lower()
    if st:
        return st
    for key in ("body", "text"):
        v = item.get(key)
        if isinstance(v, str) and v and _is_placeholder(v):
            return "preparing"
    return ""


# file-reader <= 0.3.5 reported "not ready yet" ONLY by putting this sentence
# in `body` (no status field existed on a text window). The extension now sends
# a real `status`, but a client in the wild talks to whatever version is
# deployed -- so the prose remains a recognised fallback rather than a trap.
_PREPARING_PROSE = "indexing in progress"


def _is_placeholder(text: str) -> bool:
    """True when `body` is the extension's human placeholder, not real content."""
    t = text.strip().lower()
    return t.startswith("(preparing") or _PREPARING_PROSE in t


def _text_of(item: dict) -> str:
    """The extracted text of one read_files item -- REAL text only.

    LIVE CONTRACT (verified against file-reader 0.3.5, 2026-08-22): the text
    arrives as `body`; `text` is what the ENGINE calls it one layer deeper, and
    reading only that returned a confident, completely empty answer. Both are
    accepted so neither layer's naming can silently blank a read.

    A placeholder sentence is NOT text: treating it as content made the poll
    loop exit on its first tick and declare a perfectly readable image empty.
    """
    for key in ("body", "text", "raw_body"):
        v = item.get(key)
        if isinstance(v, str) and v and not _is_placeholder(v):
            return v
    return ""


async def _upload(client, name: str, mime: str, data: bytes) -> dict:
    """Hand the bytes to the system file-reader extension. Returns its response."""
    b64 = base64.b64encode(data).decode("ascii")
    return await client.run_tool(
        "file-reader", "receive_files",
        {"files": [{"name": name, "mime_type": mime,
                    "data_base64": b64, "size": len(data)}]})


async def _read_window(client, file_id: str, offset: int, limit: int) -> dict:
    """One text window for an already-uploaded file."""
    resp = await client.run_tool(
        "file-reader", "read_files",
        {"file_ids": [file_id], "offset": max(0, offset), "limit": limit})
    return _first_item(resp)


async def _read_when_ready(client, file_id: str, offset: int, limit: int) -> dict:
    """Read a window, waiting out the engine's brief 'preparing' phase.

    A just-uploaded document is extracted asynchronously. Rather than telling
    the agent "still indexing, try again" (which burns a whole turn), poll a
    few seconds here -- in a worker thread, so nothing in the UI blocks.
    """
    item = await _read_window(client, file_id, offset, limit)
    for delay in _POLL_DELAYS:
        # LIVE LESSON (2026-08-22): "not ready yet" is NOT always the literal
        # status "preparing" -- a freshly uploaded file answers with status
        # None and an EMPTY body while the engine is still working (an image
        # runs OCR + vision, which takes seconds longer than a docx). Keying
        # the wait on the label alone exited instantly and reported "no
        # readable text" about a file that was seconds from being perfect.
        # So: wait for the TEXT, not for a word.
        if _text_of(item) or _status_of(item) == "error":
            break
        await asyncio.sleep(delay)
        item = await _read_window(client, file_id, offset, limit)
    return item


async def read_native_async(client, abs_path: str, rel: str, *,
                            offset: int = 0, limit: int = 0) -> dict:
    """Read ONE local document/image through the platform File Reader.

    `offset`/`limit` are CHARACTER coordinates (a page has no line numbers),
    which is also what the extension's own paging contract uses.

    Always returns a result dict -- ``ok: False`` with a readable ``content``
    sentence when something genuinely could not be done, never an exception.
    """
    kind, mime = classify(rel)
    if not mime:
        mime = guess_mime(rel)
    name = os.path.basename(abs_path) or "file"
    size = os.path.getsize(abs_path)

    if size > MAX_UPLOAD_BYTES:
        return {"ok": False, "native": True, "kind": kind,
                "content": (f"⟦ {rel} · {human_size(size)} · {kind} ⟧\n"
                            f"Too large to read inline (limit {human_size(MAX_UPLOAD_BYTES)}). "
                            f"Upload it in the panel's File Reader and read it by file_id, "
                            f"or point me at a smaller export.")}

    with open(abs_path, "rb") as f:
        data = f.read()

    up = await _upload(client, name, mime, data)
    file_id = extract_file_id(up)
    if not file_id:
        return {"ok": False, "native": True, "kind": kind,
                "content": (f"⟦ {rel} · {human_size(size)} · {kind} ⟧\n"
                            f"The document service did not accept this file, so there is no "
                            f"text to show. It stays readable as raw bytes only.")}

    window = limit if limit > 0 else DEFAULT_WINDOW_CHARS
    item = await _read_when_ready(client, file_id, offset, window)
    return _shape(item, rel=rel, kind=kind, size=size, file_id=file_id,
                  offset=offset, window=window)


def _shape(item: dict, *, rel: str, kind: str, size: int, file_id: str,
           offset: int, window: int) -> dict:
    """Turn one file-reader result into the read_file-shaped dict the brain sees.

    The header is the SAME visual language as a source read (⟦ … ⟧) so the
    agent needs no new mental model, and it always states how to get the rest
    -- a document the agent cannot page through is a document it will re-read
    wholesale, which is exactly the token burn this path exists to avoid.
    """
    status = _status_of(item)
    text = _text_of(item)
    head = f"⟦ {rel} · {human_size(size)} · {kind} via File Reader ⟧"

    if status == "preparing":
        return {"ok": True, "native": True, "kind": kind, "file_id": file_id,
                "status": "preparing", "content": (
                    f"{head}\nStill being extracted by the document service. "
                    f"Read it again in a moment — it is stored as file_id={file_id}.")}
    if status == "error" or (not text and status not in ("ok", "")):
        msg = str(item.get("message") or "the document service could not extract text")
        return {"ok": False, "native": True, "kind": kind, "file_id": file_id,
                "content": f"{head}\nNo text extracted: {msg}"}
    if not text:
        return {"ok": True, "native": True, "kind": kind, "file_id": file_id,
                "content": (f"{head}\nNo readable text in this file "
                            f"(it may be blank, or purely decorative).")}

    total = item.get("total_chars")
    returned = item.get("returned_chars") or len(text)
    has_more = bool(item.get("has_more"))
    bits = []
    if isinstance(total, int) and total > 0:
        bits.append(f"chars {offset + 1}-{offset + returned} of {total}")
    elif offset:
        bits.append(f"from char {offset + 1}")
    if has_more:
        bits.append(f"more follows — read again with offset={offset + returned}")
    if file_id:
        bits.append(f"file_id={file_id}")
    if bits:
        head += " [" + "; ".join(bits) + "]"

    return {"ok": True, "native": True, "kind": kind, "file_id": file_id,
            "status": "ok", "content": head + "\n" + text,
            "shown_from": offset + 1, "shown_to": offset + returned,
            "truncated": has_more, "total_chars": total}


def read_native(client_factory, abs_path: str, rel: str, *,
                offset: int = 0, limit: int = 0) -> dict:
    """Synchronous entry point for LocalToolExecutor (which runs in a worker
    thread, never on the event loop -- so a fresh loop here is safe and cannot
    deadlock the REPL).

    ``client_factory`` is a zero-arg callable returning an ImperalClient; it is
    a factory (not a client) so a plain local run with no cloud reach simply
    has none, and this whole path degrades to one honest sentence.
    """
    kind, _ = classify(rel)
    try:
        size = os.path.getsize(abs_path)
    except OSError:
        size = 0
    if client_factory is None:
        return {"ok": False, "native": True, "kind": kind, "content": (
            f"⟦ {rel} · {human_size(size)} · {kind} ⟧\n"
            f"This is not a text file. Reading it needs the platform's File Reader, "
            f"which this session cannot reach right now.")}
    try:
        client = client_factory()
        return asyncio.run(read_native_async(client, abs_path, rel,
                                             offset=offset, limit=limit))
    except Exception as e:  # noqa: BLE001 -- a read must never crash the turn
        return {"ok": False, "native": True, "kind": kind, "content": (
            f"⟦ {rel} · {human_size(size)} · {kind} ⟧\n"
            f"Could not read it through the document service: {type(e).__name__}: {e}")}
