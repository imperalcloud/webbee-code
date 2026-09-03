import base64
import os
import pytest
from unittest.mock import MagicMock, AsyncMock

from webbee.tools import LocalToolExecutor


def test_view_image_missing_file(tmp_path):
    ex = LocalToolExecutor(str(tmp_path))
    res = ex.run("view_image", {"path": "nonexistent.png"})
    assert not res["ok"]
    assert "not found" in res["content"]


def test_view_image_native_vision(tmp_path):
    ex = LocalToolExecutor(str(tmp_path))
    img_file = tmp_path / "sample.png"
    # Write a tiny PNG 1x1
    png_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
        b"\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc`\x00\x00\x00\x02\x00\x01H\xaf"
        b"\xa4q\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    img_file.write_bytes(png_bytes)

    res = ex.run("view_image", {"path": "sample.png"})
    assert res["ok"]
    assert res.get("native_vision") is True
    assert res.get("mime_type") == "image/png"
    assert res.get("size") == len(png_bytes)
    assert res.get("image_data") == base64.b64encode(png_bytes).decode("ascii")
    assert "Native Vision Image Payload" in res["content"]


def test_web_search_missing_query(tmp_path):
    ex = LocalToolExecutor(str(tmp_path))
    res = ex.run("web_search", {"query": ""})
    assert not res["ok"]
    assert "argument is missing" in res["content"]


def test_web_search_no_client_factory(tmp_path):
    ex = LocalToolExecutor(str(tmp_path), client_factory=None)
    res = ex.run("web_search", {"query": "python 3.14"})
    assert not res["ok"]
    assert "needs cloud reach" in res["content"]


def test_web_search_success_formatting(tmp_path):
    mock_client = MagicMock()
    mock_client.run_tool = AsyncMock(return_value={
        "ok": True,
        "data": {
            "items": [
                {
                    "title": "Imperal Cloud",
                    "url": "https://imperal.io",
                    "snippet": "First ICNLI AI Cloud OS"
                }
            ]
        }
    })
    ex = LocalToolExecutor(str(tmp_path), client_factory=lambda: mock_client)
    res = ex.run("web_search", {"query": "imperal cloud"})
    assert res["ok"]
    assert "Imperal Cloud" in res["content"]
    assert "https://imperal.io" in res["content"]
    assert "First ICNLI AI Cloud OS" in res["content"]


def test_read_url_no_client_factory(tmp_path):
    ex = LocalToolExecutor(str(tmp_path), client_factory=None)
    res = ex.run("read_url", {"url": "https://example.com"})
    assert not res["ok"]
    assert "needs cloud reach" in res["content"]


def test_read_url_success(tmp_path):
    mock_client = MagicMock()
    mock_client.run_tool = AsyncMock(return_value={
        "ok": True,
        "data": {
            "content": "# Example Domain\nThis domain is for use in illustrative examples.",
            "title": "Example Domain"
        }
    })
    ex = LocalToolExecutor(str(tmp_path), client_factory=lambda: mock_client)
    res = ex.run("read_url", {"url": "https://example.com"})
    assert res["ok"]
    assert "Example Domain" in res["content"]
