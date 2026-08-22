"""native-document-read-v1 — classification + the local->File Reader bridge.

What these lock down:
* a .docx/.pdf/.xlsx/.png is recognised as native, a .py/.svg/.json is NOT
  (a source file must keep its byte-exact text path -- edit_file depends on it);
* bytes reach the SYSTEM file-reader extension and NEVER the model context;
* the brain gets a compact window plus a file_id it can page with;
* every failure mode degrades to one honest sentence, never a traceback.
"""
import asyncio

import pytest

from webbee.native_files import classify, human_size, is_native
from webbee import native_read


# ── classification ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,kind", [
    ("report.pdf", "document"),
    ("Отчёт.DOCX", "document"),      # case + non-ascii name
    ("book.odt", "document"),
    ("budget.xlsx", "document"),
    ("deck.pptx", "document"),
    ("legacy.doc", "document"),
    ("shot.png", "image"),
    ("photo.JPEG", "image"),
    ("scan.heic", "image"),
])
def test_native_formats_are_recognised(name, kind):
    assert classify(name)[0] == kind
    assert is_native(name) is True


@pytest.mark.parametrize("name", [
    "tools.py", "README.md", "data.json", "icon.svg", "notes.txt",
    "Makefile", "styles.css", "query.sql", "config.yaml",
])
def test_source_and_text_files_stay_on_the_text_path(name):
    """The byte-exact read must not be hijacked: edit_file matches on it."""
    assert classify(name)[0] == "text"
    assert is_native(name) is False


def test_svg_is_text_not_image():
    """SVG is markup an agent edits -- routing it to OCR would be absurd."""
    assert classify("logo.svg") == ("text", "")


def test_human_size_is_readable():
    assert human_size(900) == "900 B"
    assert human_size(1536) == "1.5 KB"
    assert human_size(5 * 1024 * 1024) == "5.0 MB"


# ── the bridge: local path -> system file-reader extension ──────────────────

class _FakeClient:
    """Stands in for ImperalClient: records every extension call verbatim."""

    def __init__(self, *, upload=None, window=None, fail_on=""):
        self.calls = []
        self._upload = upload if upload is not None else {
            "data": {"items": [{"file_id": "fid-42", "filename": "x"}]}}
        self._window = window if window is not None else {
            "data": {"items": [{"file_id": "fid-42", "status": "ok",
                                "text": "CONTRACT TEXT", "total_chars": 13,
                                "returned_chars": 13, "has_more": False}]}}
        self._fail_on = fail_on

    async def run_tool(self, app_id, function, params):
        self.calls.append((app_id, function, params))
        if self._fail_on and function == self._fail_on:
            raise RuntimeError("engine unavailable")
        return self._upload if function == "receive_files" else self._window


def _write(tmp_path, name, data=b"%PDF-1.4 binary"):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def test_document_is_uploaded_through_the_system_file_reader(tmp_path):
    """The ONE door: bytes go to the file-reader extension, nowhere else."""
    c = _FakeClient()
    path = _write(tmp_path, "report.pdf")
    out = native_read.read_native(lambda: c, path, "report.pdf")

    assert out["ok"] is True
    apps = {app for app, _fn, _p in c.calls}
    assert apps == {"file-reader"}, "must never reach the engine directly"
    assert [fn for _a, fn, _p in c.calls][:2] == ["receive_files", "read_files"]


def test_uploaded_bytes_never_enter_the_model_context(tmp_path):
    """Base64 rides the extension call; the brain sees TEXT, not the payload."""
    c = _FakeClient()
    path = _write(tmp_path, "report.pdf", b"\x00\x01SECRETBYTES\xff")
    out = native_read.read_native(lambda: c, path, "report.pdf")

    assert "SECRETBYTES" not in out["content"]
    assert "data_base64" not in out["content"]
    up = next(p for _a, fn, p in c.calls if fn == "receive_files")
    assert up["files"][0]["data_base64"], "bytes must travel out-of-band"


def test_brain_gets_a_pageable_handle_and_a_window(tmp_path):
    c = _FakeClient(window={"data": {"items": [{
        "file_id": "fid-42", "status": "ok", "text": "PAGE ONE",
        "total_chars": 5000, "returned_chars": 8, "has_more": True}]}})
    out = native_read.read_native(lambda: c, _write(tmp_path, "big.pdf"), "big.pdf")

    assert "PAGE ONE" in out["content"]
    assert out["file_id"] == "fid-42"
    assert out["truncated"] is True
    assert "offset=" in out["content"], "must tell the agent how to page on"


def test_offset_and_limit_are_forwarded_as_character_coordinates(tmp_path):
    c = _FakeClient()
    native_read.read_native(lambda: c, _write(tmp_path, "d.docx"), "d.docx",
                            offset=500, limit=250)
    rd = next(p for _a, fn, p in c.calls if fn == "read_files")
    assert rd["offset"] == 500 and rd["limit"] == 250


def test_image_takes_the_same_path_and_returns_its_reading(tmp_path):
    """A screenshot must produce OCR/vision text, not a decode error."""
    c = _FakeClient(window={"data": {"items": [{
        "file_id": "img-7", "status": "ok",
        "text": "A dashboard screenshot showing 3 red alerts.",
        "total_chars": 44, "returned_chars": 44, "has_more": False}]}})
    out = native_read.read_native(lambda: c, _write(tmp_path, "shot.png", b"\x89PNG\r\n"), "shot.png")

    assert out["ok"] is True and out["kind"] == "image"
    assert "red alerts" in out["content"]


def test_no_cloud_reach_degrades_to_one_honest_sentence(tmp_path):
    out = native_read.read_native(None, _write(tmp_path, "report.pdf"), "report.pdf")
    assert out["ok"] is False
    assert "File Reader" in out["content"]
    assert "Traceback" not in out["content"]


def test_engine_failure_is_reported_not_raised(tmp_path):
    c = _FakeClient(fail_on="receive_files")
    out = native_read.read_native(lambda: c, _write(tmp_path, "report.pdf"), "report.pdf")
    assert out["ok"] is False
    assert "Traceback" not in out["content"]


def test_still_extracting_says_so_instead_of_looking_empty(tmp_path):
    c = _FakeClient(window={"data": {"items": [
        {"file_id": "fid-42", "status": "preparing", "text": ""}]}})
    out = native_read.read_native(lambda: c, _write(tmp_path, "report.pdf"), "report.pdf")
    assert "fid-42" in out["content"]
    assert "moment" in out["content"].lower() or "again" in out["content"].lower()


def test_oversized_file_is_refused_before_the_upload(tmp_path):
    big = b"x" * (native_read.MAX_UPLOAD_BYTES + 1)
    c = _FakeClient()
    out = native_read.read_native(lambda: c, _write(tmp_path, "huge.pdf", big), "huge.pdf")
    assert out["ok"] is False
    assert c.calls == [], "must not attempt a doomed upload"


# ── the placeholder trap (live lesson, 2026-08-22) ──────────────────────────

def test_preparing_placeholder_is_not_mistaken_for_content():
    """file-reader <=0.3.5 puts a HUMAN sentence in `body` while a file is
    still being extracted, with no status field. Reading that as text made the
    poll loop exit on tick one and declare a perfectly readable image empty."""
    item = {"body": "(preparing — indexing in progress, ask again in a moment)"}
    assert native_read._text_of(item) == ""
    assert native_read._status_of(item) == "preparing"


def test_explicit_status_field_is_honoured():
    """Newer file-reader reports state properly -- prefer the field."""
    assert native_read._status_of({"status": "preparing", "body": ""}) == "preparing"
    assert native_read._status_of({"status": "ok", "body": "hi"}) == "ok"


def test_real_text_is_returned_from_body_first():
    """`body` is the extension's field name; `text` is the engine's. Both work."""
    assert native_read._text_of({"body": "REAL", "text": "deeper"}) == "REAL"
    assert native_read._text_of({"text": "deeper"}) == "deeper"


def test_poll_waits_for_text_then_returns_it(monkeypatch):
    """The wait must survive a slow extraction (an image runs OCR + vision)."""
    import asyncio
    seq = [{"body": "(preparing — indexing in progress, ask again in a moment)"},
           {"body": "(preparing — indexing in progress, ask again in a moment)"},
           {"body": "FINALLY READY", "status": "ok", "total_chars": 13,
            "returned_chars": 13, "has_more": False}]

    async def _fake_window(client, file_id, offset, limit):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(native_read, "_read_window", _fake_window)
    monkeypatch.setattr(native_read, "_POLL_DELAYS", (0.01, 0.01, 0.01))
    item = asyncio.run(native_read._read_when_ready(None, "fid", 0, 100))
    assert native_read._text_of(item) == "FINALLY READY"
