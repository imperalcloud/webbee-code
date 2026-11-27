"""A result fits the door that takes it (2026-11-27).

Live: a view_image result of 1 153 322 bytes came back 413 -- the gateway takes a body of 1 MiB while the client cut
a result at 4 MB, so its cut never ran. The bound is the door's; an image over it is sent smaller and said so."""
import base64
import io
import os

import pytest

from webbee import session as S
from webbee import tools as T

DOOR_BYTES = 1024 * 1024          # nginx client_max_body_size 1m on the gateway, measured 2026-11-27


def test_the_result_bound_is_under_the_door():
    assert S._RESULT_MAX_BYTES < DOOR_BYTES
    assert T.IMAGE_MAX_BYTES * 4 // 3 + 64 * 1024 < S._RESULT_MAX_BYTES     # base64 of an image, and text beside it


def test_a_result_over_the_door_is_cut_and_names_the_bound_in_its_own_unit():
    out = {"req_id": "r1", "result": {"ok": True, "native_vision": True, "image_data": "A" * 1_153_000,
                                      "content": "pic"}}
    capped = S._cap_result(out)
    assert S._json_bytes(capped) <= S._RESULT_MAX_BYTES and "image_data" not in capped["result"]
    assert "960 KB a result can carry" in capped["result"]["content"] and "0 MB" not in capped["result"]["content"]
    assert S._size_words(4 * 1024 * 1024) == "4 MB" and S._size_words(100_000) == "97 KB"


def _noise_png(side):
    Image = pytest.importorskip("PIL.Image")
    im = Image.frombytes("RGB", (side, side), os.urandom(side * side * 3))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_an_image_over_the_bound_is_sent_smaller_whole_and_said(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    raw = _noise_png(900)
    assert len(raw) > T.IMAGE_MAX_BYTES
    (tmp_path / "big.png").write_bytes(raw)
    out = T.LocalToolExecutor(str(tmp_path))._t_view_image({"path": "big.png"})
    sent = base64.b64decode(out["image_data"])
    assert out["ok"] and out["mime_type"] == "image/jpeg" and len(sent) <= T.IMAGE_MAX_BYTES
    w, h = Image.open(io.BytesIO(sent)).size
    assert w == h and f"shown as {w}x{h} JPEG" in out["content"]            # the whole picture, its sides kept
    assert S._cap_result({"req_id": "r", "result": out})["result"].get("image_data")   # and it passes the door


def test_a_small_image_goes_as_it_is(tmp_path):
    raw = _noise_png(64)
    (tmp_path / "s.png").write_bytes(raw)
    out = T.LocalToolExecutor(str(tmp_path))._t_view_image({"path": "s.png"})
    assert base64.b64decode(out["image_data"]) == raw and "shown as" not in out["content"]


def test_what_cannot_be_shrunk_is_left_to_the_cut(monkeypatch):
    assert T.shrink_image(b"not an image at all" * 100) is None


def test_an_image_is_decoded_through_one_door_raster_only_and_bounded(monkeypatch):
    """Security review 2026-11-27: a workspace file is not trusted -- no EPS/PS (Pillow runs Ghostscript on them),
    no canvas that does not fit in memory."""
    pytest.importorskip("PIL.Image")
    import pathlib
    from webbee import media
    eps = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\n%%EndComments\nshowpage\n%%EOF\n"
    with pytest.raises(ValueError):
        media.open_image(io.BytesIO(eps))
    assert T.shrink_image(eps * 20000) is None
    assert not {"EPS", "PDF", "TIFF"} & set(media.IMAGE_FORMATS)
    monkeypatch.setattr(media, "IMAGE_MAX_PIXELS", 1000)
    with pytest.raises(ValueError):
        media.open_image(io.BytesIO(_noise_png(64)))
    src = pathlib.Path(media.__file__).parent
    opens = [f.name for f in src.glob("*.py") if "Image.open(" in f.read_text(encoding="utf-8")]
    assert opens == ["media.py"]                       # nobody decodes an image beside the door
