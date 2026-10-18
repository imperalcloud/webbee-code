"""inspect_page: look at a web page in the local browser over the Chrome DevTools Protocol.

One call returns what the kernel needs to MEASURE a screen (truth-loop stage 10): per viewport
width, the visible element boxes with their roles, text, z-index and computed colors (the painted
background behind each, not a guess), the page's scroll width, and one screenshot. The agent no
longer writes its own websocket scripts against port 9222 (live: 56 of 205 shell calls of one
UI task were such scripts).

Stdlib only: a minimal RFC 6455 client (text frames, client masking, ping/pong) -- no new
dependency for a verb most sessions never call.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time
import urllib.parse
import urllib.request

DEFAULT_PORT = 9222
DEFAULT_WIDTHS = (375, 768, 1440)
VIEW_HEIGHT = 900
MAX_ELEMENTS = 400
_TIMEOUT = 20.0

# the page side: visible boxes worth measuring, in page coordinates, with the painted background
_COLLECT_JS = r"""
(() => {
  const MAX = %d;
  const vw = window.innerWidth, vh = window.innerHeight;
  const sx = window.scrollX, sy = window.scrollY;
  const IMPLICIT = {A: 'link', BUTTON: 'button', INPUT: 'input', SELECT: 'select', TEXTAREA: 'textbox',
                    H1: 'h1', H2: 'h2', H3: 'h3', IMG: 'img', NAV: 'navigation', HEADER: 'banner',
                    FOOTER: 'contentinfo', MAIN: 'main', LABEL: 'label', P: 'text', LI: 'listitem'};
  const painted = (el) => {
    for (let e = el; e && e.nodeType === 1; e = e.parentElement) {
      const c = getComputedStyle(e).backgroundColor;
      if (c && c !== 'transparent' && !/rgba\([^)]*,\s*0\)$/.test(c)) return c;
    }
    return 'rgb(255, 255, 255)';
  };
  const ownText = (el) => Array.from(el.childNodes).filter(n => n.nodeType === 3)
                              .map(n => n.textContent).join(' ').replace(/\s+/g, ' ').trim();
  const out = [];
  const all = document.body ? document.body.querySelectorAll('*') : [];
  for (const el of all) {
    if (out.length >= MAX) break;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || parseFloat(cs.opacity) === 0) continue;
    const role = (el.getAttribute('role') || IMPLICIT[el.tagName] || '').toLowerCase();
    const text = ownText(el);
    const handlers = !!(el.onclick || el.getAttribute('onclick') || cs.cursor === 'pointer');
    if (!role && !text && !handlers) continue;
    out.push({
      id: el.id || (el.getAttribute('data-testid') || '') || (el.tagName.toLowerCase() + '_' + out.length),
      tag: el.tagName.toLowerCase(), role: role, text: (text || el.getAttribute('aria-label') || el.alt || '').slice(0, 100),
      x: Math.round(r.left + sx), y: Math.round(r.top + sy), w: Math.round(r.width), h: Math.round(r.height),
      z_index: parseInt(cs.zIndex) || 0, color: cs.color, background: painted(el),
      font_size: parseFloat(cs.fontSize) || 0, font_weight: parseInt(cs.fontWeight) || 400,
      has_handlers: handlers,
    });
  }
  return JSON.stringify({width: vw, height: vh, scroll_width: document.documentElement.scrollWidth,
                         title: document.title, url: location.href, elements: out});
})()
""" % MAX_ELEMENTS


class CDPError(Exception):
    pass


class _WebSocket:
    """A minimal websocket client: enough for CDP (text frames in, text frames out)."""

    def __init__(self, url: str, timeout: float = _TIMEOUT):
        u = urllib.parse.urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (f"?{u.query}" if u.query else "")
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
                          .encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError("the browser closed the connection during the handshake")
            head += chunk
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in status:
            raise CDPError(f"websocket handshake refused: {status}")
        self.buf = head.split(b"\r\n\r\n", 1)[1]

    def _recv_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self.buf)))
            if not chunk:
                raise CDPError("the browser closed the connection")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text: str) -> None:
        data = text.encode()
        head = bytearray([0x81])
        n = len(data)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(bytes(head) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _send_control(self, opcode: int, payload: bytes = b"") -> None:
        mask = os.urandom(4)
        self.sock.sendall(bytes([0x80 | opcode, 0x80 | len(payload)]) + mask
                          + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv(self) -> str:
        parts = []
        while True:
            b1, b2 = self._recv_exact(2)
            opcode, n = b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if b2 & 0x80 else b""
            data = self._recv_exact(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x9:
                self._send_control(0xA, data)
                continue
            if opcode == 0x8:
                raise CDPError("the browser closed the page connection")
            if opcode in (0x1, 0x2, 0x0):
                parts.append(data)
                if b1 & 0x80:
                    return b"".join(parts).decode(errors="replace")

    def close(self) -> None:
        try:
            self._send_control(0x8)
            self.sock.close()
        except OSError:
            pass


class _Page:
    def __init__(self, ws_url: str):
        self.ws = _WebSocket(ws_url)
        self.n = 0

    def call(self, method: str, params: dict | None = None, timeout: float = _TIMEOUT) -> dict:
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise CDPError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result") or {}
        raise CDPError(f"{method}: no answer in {timeout:.0f}s")

    def evaluate(self, expr: str):
        res = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        if res.get("exceptionDetails"):
            raise CDPError(f"page script failed: {res['exceptionDetails'].get('text')}")
        return (res.get("result") or {}).get("value")

    def wait_loaded(self, timeout: float = 15.0) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.evaluate("document.readyState") == "complete":
                return
            time.sleep(0.25)


def _http_json(port: int, path: str, method: str = "GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode() or "null")


def _pick_tab(port: int, url: str, tab: str) -> dict:
    tabs = [t for t in _http_json(port, "/json/list") or [] if t.get("type") == "page"]
    if tab:
        for t in tabs:
            if tab in (t.get("id"), ) or tab in str(t.get("url") or "") or tab in str(t.get("title") or ""):
                return t
        raise CDPError(f"no open tab matches {tab!r} (open: {', '.join(str(t.get('url'))[:60] for t in tabs[:8])})")
    if url:
        for t in tabs:
            if str(t.get("url") or "").rstrip("/") == url.rstrip("/"):
                return t
        quoted = urllib.parse.quote(url, safe=":/?&=#%")
        try:
            return _http_json(port, f"/json/new?{quoted}", method="PUT")
        except Exception:
            return _http_json(port, f"/json/new?{quoted}")
    if not tabs:
        raise CDPError("the browser has no open page; pass url")
    return tabs[0]


def inspect_page(url: str = "", tab: str = "", widths=None, screenshot: bool = True,
                 port: int = DEFAULT_PORT) -> dict:
    """The page's boxes per width and one screenshot (at the widest width), or ``ok: False`` with why."""
    widths = [int(w) for w in (widths or DEFAULT_WIDTHS) if 200 <= int(w) <= 4000][:5] or list(DEFAULT_WIDTHS)
    try:
        target = _pick_tab(port, url, tab)
    except (OSError, ValueError) as e:
        return {"ok": False, "content": f"inspect_page: no browser with remote debugging on port {port} ({e}). "
                                        f"Start Chrome/Brave with --remote-debugging-port={port}."}
    except CDPError as e:
        return {"ok": False, "content": f"inspect_page: {e}"}
    page = None
    try:
        page = _Page(target["webSocketDebuggerUrl"])
        page.call("Page.enable")
        if url and str(target.get("url") or "").rstrip("/") != url.rstrip("/"):
            page.call("Page.navigate", {"url": url})
        elif url:
            page.call("Page.reload", {"ignoreCache": True})
        page.wait_loaded()
        viewports, title, href = [], "", url
        for w in widths:
            page.call("Emulation.setDeviceMetricsOverride",
                      {"width": w, "height": VIEW_HEIGHT, "deviceScaleFactor": 1, "mobile": w < 768})
            time.sleep(0.4)          # layout settles (media queries, transitions)
            got = json.loads(page.evaluate(_COLLECT_JS) or "{}")
            viewports.append({"width": w, "height": got.get("height") or VIEW_HEIGHT,
                              "scroll_width": got.get("scroll_width"), "elements": got.get("elements") or []})
            title, href = got.get("title") or "", got.get("url") or url
        png = ""
        if screenshot:
            shot = page.call("Page.captureScreenshot", {"format": "jpeg", "quality": 60})
            png = "data:image/jpeg;base64," + str(shot.get("data") or "") if shot.get("data") else ""
        page.call("Emulation.clearDeviceMetricsOverride")
    except (CDPError, OSError, ValueError, KeyError) as e:
        return {"ok": False, "content": f"inspect_page: {e}"}
    finally:
        if page is not None:
            page.ws.close()
    summary = "; ".join(f"{v['width']}px: {len(v['elements'])} boxes"
                        + (f", page {v['scroll_width']}px wide" if (v.get("scroll_width") or 0) > v["width"] else "")
                        for v in viewports)
    return {"ok": True, "content": f"inspect_page {href}: {summary}"
                                   + (f"; screenshot at {widths[-1]}px" if png else ""),
            "url": href, "title": title, "viewports": viewports, "png_base64": png}
