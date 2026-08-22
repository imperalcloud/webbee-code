# Copyright (c) 2026 Imperal, Inc.
# Licensed under the AGPL-3.0 License.
"""Native document & image reading for the terminal agent.

WHY THIS EXISTS
---------------
``read_file`` opened every path as UTF-8 text. A .docx, .pdf, .xlsx or a
screenshot therefore did not read "badly" -- it raised UnicodeDecodeError and
the agent got nothing at all, on every OS. Meanwhile the platform ALREADY
owns a document service (the doc-extractor engine behind the *system* File
Reader extension) that handles pdf/docx/xlsx/odt/pptx and images (OCR +
vision). The gap was purely the missing bridge from a LOCAL path to that
service.

DESIGN (three rules that shape everything here)
1. NO parsers on the client, none in the kernel. The engine is the single
   source of truth for extraction -- reached ONLY through the system
   file-reader extension, never by talking to the engine directly.
2. Bytes must never travel through the model's context. The file is uploaded
   out-of-band (the same door the panel dropzone, Telegram and clipboard
   paste already use); the brain receives a compact TEXT window plus a
   ``file_id`` handle it can page through -- so a 300-page PDF costs a
   window, not a fortune in tokens.
3. Honest degradation. No cloud reach, no plan, engine down, still indexing:
   each returns a plain readable sentence saying what happened and what the
   agent can do next -- never a stack trace, never a silent empty read.
"""
from __future__ import annotations

import os

# Formats the engine extracts. Value = mime type sent at upload; the ENGINE
# re-sniffs content itself, so a wrong-but-plausible mime never decides the
# outcome -- this is a hint, not a contract.
DOCUMENT_MIMES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".rtf": "application/rtf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".epub": "application/epub+zip",
}

IMAGE_MIMES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".heic": "image/heic",
}

# Product policy of the file-reader dropzone (providers/lifecycle.py).
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

# .svg is genuine text (XML) -- readable as source. Keeping it OUT of the
# native path means `read_file` on an icon still shows editable markup.
_TEXT_FIRST = {".svg"}


def classify(path: str) -> tuple[str, str]:
    """(kind, mime) for a path. kind: 'document' | 'image' | 'text'.

    Extension-based and deliberately conservative: anything unknown stays
    'text' so the normal UTF-8 read (and its byte-exact edit contract) is
    never disturbed. A binary that slips through is caught at read time by
    the UnicodeDecodeError fallback, which routes here anyway.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in _TEXT_FIRST:
        return "text", ""
    if ext in DOCUMENT_MIMES:
        return "document", DOCUMENT_MIMES[ext]
    if ext in IMAGE_MIMES:
        return "image", IMAGE_MIMES[ext]
    return "text", ""


def guess_mime(path: str) -> str:
    """Best-effort mime for a path the caller already knows is not text."""
    ext = os.path.splitext(path)[1].lower()
    return (DOCUMENT_MIMES.get(ext) or IMAGE_MIMES.get(ext)
            or "application/octet-stream")


def is_native(path: str) -> bool:
    """True when the path is a document/image the engine should extract."""
    return classify(path)[0] in ("document", "image")


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"
