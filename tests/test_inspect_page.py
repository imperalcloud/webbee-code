"""inspect_page (truth-loop stage 10, client side): one CDP call gives the kernel the page's boxes,
roles and computed colors per viewport width, and one screenshot.

A fake browser on a local port speaks the real wire protocol (HTTP /json/list, a websocket
upgrade, masked client frames, unmasked server frames), so the framing is tested end to end."""
import base64
import hashlib
import json
import socket
import struct
import threading

from webbee import browser
from webbee.tools import LocalToolExecutor


def _frame(text: str) -> bytes:
    data = text.encode()
    n = len(data)
    if n < 126:
        return bytes([0x81, n]) + data
    if n < 65536:
        return bytes([0x81, 126]) + struct.pack(">H", n) + data
    return bytes([0x81, 127]) + struct.pack(">Q", n) + data


def _read_frame(conn) -> str:
    def exact(k):
        buf = b""
        while len(buf) < k:
            chunk = conn.recv(k - len(buf))
            if not chunk:
                raise ConnectionError
            buf += chunk
        return buf
    b1, b2 = exact(2)
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack(">H", exact(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", exact(8))[0]
    assert b2 & 0x80, "a client frame must be masked"
    mask = exact(4)
    data = bytes(b ^ mask[i % 4] for i, b in enumerate(exact(n)))
    if b1 & 0x0F == 0x8:
        raise ConnectionError
    return data.decode()


class FakeBrowser:
    """One tab; the page is 412px wide below 400px (a broken mobile layout)."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.width = 1440
        self.calls = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        req = b""
        while b"\r\n\r\n" not in req:
            req += conn.recv(4096)
        line = req.split(b"\r\n", 1)[0].decode()
        if "Upgrade: websocket" in req.decode():
            key = [h.split(":", 1)[1].strip() for h in req.decode().split("\r\n") if h.startswith("Sec-WebSocket-Key")][0]
            acc = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            conn.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                         f"Sec-WebSocket-Accept: {acc}\r\n\r\n".encode())
            try:
                while True:
                    msg = json.loads(_read_frame(conn))
                    self.calls.append(msg["method"])
                    conn.sendall(_frame(json.dumps({"id": msg["id"], "result": self._answer(msg)})))
            except (ConnectionError, OSError):
                conn.close()
            return
        body = json.dumps([{"id": "T1", "type": "page", "url": "http://localhost:3000/", "title": "Home",
                            "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/page/T1"}])
        if "/json/new" in line:
            body = json.dumps({"id": "T2", "type": "page", "url": "about:blank",
                               "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/page/T2"})
        conn.sendall(f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n{body}".encode())
        conn.close()

    def _answer(self, msg):
        m, p = msg["method"], msg.get("params") or {}
        if m == "Emulation.setDeviceMetricsOverride":
            self.width = p["width"]
        if m == "Runtime.evaluate":
            if p["expression"] == "document.readyState":
                return {"result": {"value": "complete"}}
            page = {"width": self.width, "height": 900, "scroll_width": 412 if self.width < 400 else self.width,
                    "title": "Home", "url": "http://localhost:3000/",
                    "elements": [{"id": "cta", "tag": "button", "role": "button", "text": "Start", "x": 10, "y": 10,
                                  "w": 120, "h": 48, "z_index": 0, "color": "rgb(255, 255, 255)",
                                  "background": "rgb(0, 0, 0)", "font_size": 16, "font_weight": 600,
                                  "has_handlers": True}]}
            return {"result": {"value": json.dumps(page)}}
        if m == "Page.captureScreenshot":
            return {"data": base64.b64encode(b"\xff\xd8jpeg" * 100).decode()}
        return {}


def test_inspect_page_returns_boxes_per_width_and_a_screenshot():
    fb = FakeBrowser()
    out = browser.inspect_page(tab="Home", port=fb.port)
    assert out["ok"] is True, out
    assert [v["width"] for v in out["viewports"]] == [375, 768, 1440]
    assert out["viewports"][0]["scroll_width"] == 412 and out["viewports"][2]["scroll_width"] == 1440
    assert out["viewports"][1]["elements"][0]["background"] == "rgb(0, 0, 0)"
    assert out["png_base64"].startswith("data:image/jpeg;base64,")
    assert "375px: 1 boxes, page 412px wide" in out["content"]
    assert fb.calls[-1] == "Emulation.clearDeviceMetricsOverride"


def test_a_url_not_open_is_opened_and_widths_are_the_callers():
    fb = FakeBrowser()
    out = browser.inspect_page(url="http://localhost:3000/settings", widths=[390], screenshot=False, port=fb.port)
    assert out["ok"] is True and [v["width"] for v in out["viewports"]] == [390]
    assert "Page.navigate" in fb.calls and out["png_base64"] == ""


def test_no_browser_says_how_to_start_one():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    out = browser.inspect_page(port=port)
    assert out["ok"] is False and f"--remote-debugging-port={port}" in out["content"]


def test_the_tool_verb_dispatches_to_the_browser(tmp_path, monkeypatch):
    fb = FakeBrowser()
    monkeypatch.setenv("WEBBEE_CDP_PORT", str(fb.port))
    out = LocalToolExecutor(str(tmp_path))._t_inspect_page({"tab": "T1", "widths": [1024], "screenshot": False})
    assert out["ok"] is True and out["viewports"][0]["width"] == 1024
