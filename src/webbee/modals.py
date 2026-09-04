"""Interactive TUI Modal Dialogs & System Native Controls for Webbee Code.

Enterprise Grade Multi-OS popups with:
- True dynamic responsive layout (macOS, Linux, Windows, BSD, small/wide terminals)
- Ultra-responsive, mutual-exclusive Mouse Hover on dialog buttons (never sticky)
- Cross-platform Audio Recording, Playback and Speech-To-Text (AVFoundation, PulseAudio/ALSA, DirectShow)
- Native OS File Picker integration (Finder on macOS, Zenity/Kdialog on Linux, PowerShell on Windows)
- Clean square bracket [ Button ] styling with hover and focus highlights
- In-terminal image and media rendering integration
"""
from __future__ import annotations

import itertools
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Callable, Optional, Tuple, List

from prompt_toolkit.widgets import Dialog, Label
from prompt_toolkit.layout.containers import HSplit, Window, Container
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.application import get_app_or_none

from webbee.media import play_audio_file, render_image_in_terminal


class HoverButton:
    """Enterprise-grade Button with clean styling and active Hover support."""

    _active_hover_btn: "HoverButton | None" = None

    def __init__(
        self,
        text: str,
        handler: Optional[Callable[[], None]] = None,
        width: Optional[int] = None,
        left_symbol: str = "[",
        right_symbol: str = "]",
    ) -> None:
        self.text = text
        self.handler = handler
        self.left_symbol = left_symbol
        self.right_symbol = right_symbol

        cwidth = get_cwidth(text) + get_cwidth(left_symbol) + get_cwidth(right_symbol) + 2
        self.width = width if width is not None else max(10, cwidth)

        kb = KeyBindings()

        @kb.add(" ")
        @kb.add("enter")
        def _click(event):
            if self.handler is not None:
                self.handler()

        self.control = FormattedTextControl(
            self._get_text_fragments,
            key_bindings=kb,
            focusable=True,
        )

        self.window = Window(
            self.control,
            height=1,
            width=self.width,
            style=self._get_style,
            dont_extend_width=False,
            dont_extend_height=True,
        )

    @property
    def is_hovered(self) -> bool:
        return HoverButton._active_hover_btn is self

    def _get_style(self) -> str:
        app = get_app_or_none()
        if app is not None and app.layout.has_focus(self):
            return "class:button.focused"
        if HoverButton._active_hover_btn is self:
            return "class:button.hover"
        return "class:button"

    def _mouse_handler(self, mouse_event: MouseEvent) -> None:
        app = get_app_or_none()
        if mouse_event.event_type == MouseEventType.MOUSE_MOVE:
            if HoverButton._active_hover_btn is not self:
                HoverButton._active_hover_btn = self
                if app is not None:
                    app.invalidate()
        elif mouse_event.event_type == MouseEventType.MOUSE_UP:
            HoverButton._active_hover_btn = None
            if self.handler is not None:
                self.handler()
            if app is not None:
                app.invalidate()

    def _get_text_fragments(self) -> StyleAndTextTuples:
        text_w = get_cwidth(self.text)
        left_w = get_cwidth(self.left_symbol)
        right_w = get_cwidth(self.right_symbol)
        padding = max(0, self.width - (text_w + left_w + right_w))
        pad_l = " " * (padding // 2)
        pad_r = " " * (padding - len(pad_l))

        full_text = f"{self.left_symbol}{pad_l}{self.text}{pad_r}{self.right_symbol}"
        return [("", full_text, self._mouse_handler)]

    def __pt_container__(self) -> Container:
        return self.window


def make_clean_button(
    text: str,
    handler: Optional[Callable[[], None]] = None,
    width: Optional[int] = None,
) -> HoverButton:
    """Create a clean button with square brackets [ Button ] and interactive hover highlight."""
    return HoverButton(text=text, handler=handler, width=width)


def open_native_file_picker() -> Optional[str]:
    """Launch native OS file picker window across macOS, Linux, and Windows."""
    if sys.platform == "darwin":
        script = 'POSIX path of (choose file with prompt "Select file to attach to Webbee Code:")'
        try:
            res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=120)
            if res.returncode == 0:
                chosen = res.stdout.strip()
                if chosen and os.path.exists(chosen):
                    return chosen
        except Exception:
            pass

    elif sys.platform.startswith("linux"):
        if shutil.which("zenity"):
            try:
                res = subprocess.run(
                    ["zenity", "--file-selection", "--title=Select file to attach to Webbee Code"],
                    capture_output=True, text=True, timeout=120
                )
                if res.returncode == 0 and res.stdout.strip():
                    return res.stdout.strip()
            except Exception:
                pass
        elif shutil.which("kdialog"):
            try:
                res = subprocess.run(
                    ["kdialog", "--getopenfilename", ".", "All Files (*)"],
                    capture_output=True, text=True, timeout=120
                )
                if res.returncode == 0 and res.stdout.strip():
                    return res.stdout.strip()
            except Exception:
                pass

    elif sys.platform in ("win32", "cygwin"):
        ps_cmd = (
            'Add-Type -AssemblyName System.Windows.Forms; '
            '$f = New-Object System.Windows.Forms.OpenFileDialog; '
            '$f.Title = "Select file to attach to Webbee Code"; '
            'if ($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $f.FileName }'
        )
        try:
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=120
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except Exception:
            pass

    return None


def get_macos_builtin_audio_device() -> str:
    """Discover the built-in microphone device index on macOS for AVFoundation.
    Prioritizes built-in MacBook / internal microphone over remote devices (iPhone Continuity).
    """
    try:
        proc = subprocess.run(
            ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", '""'],
            capture_output=True, text=True, timeout=5
        )
        out = proc.stderr
        in_audio = False
        candidates = []
        for line in out.splitlines():
            if "AVFoundation audio devices:" in line:
                in_audio = True
                continue
            if in_audio:
                m = re.search(r"\[(\d+)\]\s+(.*)", line)
                if m:
                    idx, name = m.group(1), m.group(2).strip()
                    candidates.append((idx, name))

        # 1. Match MacBook / Built-in / Internal explicitly first
        for idx, name in candidates:
            nl = name.lower()
            if "macbook" in nl or "built-in" in nl or "internal" in nl:
                return f":{idx}"

        # 2. Match any non-iPhone microphone
        for idx, name in candidates:
            nl = name.lower()
            if "iphone" not in nl and ("microphone" in nl or "mic" in nl or "audio" in nl):
                return f":{idx}"

        if candidates:
            return f":{candidates[0][0]}"
        return ":none"
    except Exception:
        return ":0"


def get_system_audio_capture_args(output_path: str) -> List[str]:
    """Build cross-platform FFmpeg audio recording CLI arguments for macOS, Linux, and Windows."""
    if sys.platform == "darwin":
        dev = get_macos_builtin_audio_device()
        return ["ffmpeg", "-y", "-f", "avfoundation", "-i", dev, "-ar", "16000", "-ac", "1", output_path]
    elif sys.platform.startswith("linux"):
        if shutil.which("pactl"):
            return ["ffmpeg", "-y", "-f", "pulse", "-i", "default", "-ar", "16000", "-ac", "1", output_path]
        return ["ffmpeg", "-y", "-f", "alsa", "-i", "default", "-ar", "16000", "-ac", "1", output_path]
    elif sys.platform in ("win32", "cygwin"):
        return ["ffmpeg", "-y", "-f", "dshow", "-i", "audio=virtual-audio-device", "-ar", "16000", "-ac", "1", output_path]
    return ["ffmpeg", "-y", "-i", "default", output_path]


def transcribe_audio_file(audio_path: str) -> str:
    """Transcribe audio file via Imperal Cloud System Voice STT endpoint (/v1/voice/stt).

    Works on ANY OS (macOS, Linux, Windows) seamlessly without local dependencies.
    """
    if not os.path.exists(audio_path) or os.path.getsize(audio_path) < 1000:
        return ""

    # 1. Primary: Official Imperal Cloud System STT (/v1/voice/stt)
    try:
        import json, urllib.request, urllib.error, uuid

        cred_path = os.path.expanduser("~/.imperal/credentials.json")
        token = ""
        if os.path.exists(cred_path):
            try:
                data = json.load(open(cred_path))
                token = data.get("access_token", "")
            except Exception:
                pass

        api_url = os.environ.get("IMPERAL_API_URL", "https://auth.imperal.io").rstrip("/")
        url = f"{api_url}/v1/voice/stt"

        with open(audio_path, "rb") as f:
            audio_bytes = f.read()

        boundary = uuid.uuid4().hex
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="audio"; filename="voice.wav"\r\n'
            f"Content-Type: audio/wav\r\n\r\n"
        ).encode("utf-8") + audio_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status == 200:
                res_data = json.loads(resp.read().decode("utf-8"))
                text = res_data.get("text", "")
                if text and text.strip():
                    return text.strip()
    except Exception:
        pass

    # 2. Local Whisper CLI fallback (if available)
    if shutil.which("whisper"):
        try:
            res = subprocess.run(
                ["whisper", audio_path, "--output_format", "txt", "--output_dir", tempfile.gettempdir()],
                capture_output=True, text=True, timeout=60
            )
            base = os.path.splitext(os.path.basename(audio_path))[0]
            txt_file = os.path.join(tempfile.gettempdir(), f"{base}.txt")
            if os.path.exists(txt_file):
                t = open(txt_file, encoding="utf-8").read().strip()
                if t:
                    return t
        except Exception:
            pass

    return ""


def create_file_picker_dialog(
    workspace_path: str,
    on_attach: Callable[[str], None],
    on_close: Callable[[], None],
) -> Dialog:
    """Create File & Document Picker Dialog with responsive layout and native OS button."""
    items: List[str] = []
    try:
        entries = sorted(os.listdir(workspace_path))
        items = [e for e in entries if not e.startswith(".") and not e.startswith("__")][:15]
    except Exception:
        items = []

    def _pick_item(name: str):
        on_close()
        on_attach(name)

    def _pick_native():
        on_close()
        chosen = open_native_file_picker()
        if chosen:
            on_attach(chosen)

    file_buttons = []
    for item in items:
        ext = os.path.splitext(item)[1].lower()
        if ext in (".pdf",):
            icon = "📄 "
        elif ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            icon = "🖼️ "
        elif ext in (".docx", ".doc", ".xlsx", ".csv", ".txt", ".md", ".py"):
            icon = "📝 "
        else:
            icon = "📎 "
        file_buttons.append(
            make_clean_button(f"{icon}{item}", handler=lambda n=item: _pick_item(n))
        )

    action_buttons = [
        make_clean_button("📂 Browse Native OS Dialog...", handler=_pick_native),
        make_clean_button("✕ Cancel", handler=on_close),
    ]

    all_elements = file_buttons + action_buttons
    app = get_app_or_none()
    cols = app.output.get_size()[1] if (app and app.output) else 80
    dialog_width = Dimension(min=36, max=76, preferred=min(68, max(36, cols - 6)))

    return Dialog(
        title="Attach File / Document",
        body=HSplit(all_elements),
        buttons=[],
        width=dialog_width,
        modal=True,
    )


def create_mic_dialog(
    on_transcribe: Callable[[str], None],
    on_send_direct: Callable[[str], None],
    on_close: Callable[[], None],
) -> Dialog:
    """Create Voice Recording (STT) dialog matching standard modal design with exactly 2 buttons: [▶ Send] and [✕ Cancel]."""
    start_time = time.time()
    voice_dir = os.path.expanduser("~/.webbee/voice")
    os.makedirs(voice_dir, exist_ok=True)
    voice_filename = f"voice_{int(time.time())}.wav"
    voice_path = os.path.join(voice_dir, voice_filename)

    rec_proc: Optional[subprocess.Popen] = None

    try:
        args = get_system_audio_capture_args(voice_path)
        rec_proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    def _anim_text():
        elapsed = max(1, int(time.time() - start_time))
        eqs = [" ▃▅▇▅▃ ", "▃▅▇▅▃ ▃", "▅▇▅▃ ▃▅", "▇▅▃ ▃▅▇", "▅▃ ▃▅▇▅"]
        frame = eqs[elapsed % len(eqs)]
        return [
            ("class:tb.mode.plan", f"\n  🎙️ Recording voice note: [ {frame} ]  Duration: {elapsed}s\n\n"),
            ("class:dialog.body", "  Click [▶ Send Voice Note] to transcribe and send to agent,\n"),
            ("class:dialog.body", "  or [✕ Cancel] to discard recording.\n"),
        ]

    anim_ctrl = FormattedTextControl(_anim_text)

    def _stop_rec() -> int:
        nonlocal rec_proc
        elapsed = max(1, int(time.time() - start_time))
        if rec_proc is not None:
            try:
                if rec_proc.poll() is None:
                    rec_proc.terminate()
                    rec_proc.wait(timeout=2)
            except Exception:
                pass
        return elapsed

    def _handle_send():
        rec_seconds = _stop_rec()
        on_close()
        # Direct STT via Imperal Cloud STT: transcribe audio into plain text instruction
        text = transcribe_audio_file(voice_path)
        if text and text.strip() and text.strip().lower() not in ("you", "thank you", "silence", "."):
            on_send_direct(text.strip())
        else:
            # If STT returns empty or silence, send informative turn
            on_send_direct(f"🎙️ [Voice note: {rec_seconds}s audio — {text.strip() if text else 'audio recorded'}]")

    def _handle_cancel():
        _stop_rec()
        try:
            if os.path.exists(voice_path):
                os.remove(voice_path)
        except Exception:
            pass
        on_close()

    # EXACTLY TWO BUTTONS: Send and Cancel matching all other dialog layouts
    buttons = [
        make_clean_button("▶ Send Voice Note", handler=_handle_send),
        make_clean_button("✕ Cancel", handler=_handle_cancel),
    ]

    all_elements = [
        Window(anim_ctrl, height=4),
        HSplit(buttons),
    ]

    app = get_app_or_none()
    cols = app.output.get_size()[1] if (app and app.output) else 80
    dialog_width = Dimension(min=36, max=76, preferred=min(68, max(36, cols - 6)))

    return Dialog(
        title="Voice Recording (STT)",
        body=HSplit(all_elements),
        buttons=[],
        width=dialog_width,
        modal=True,
    )


def create_session_menu_dialog(
    current_remote: str,
    on_set_remote: Callable[[str], None],
    on_reset_conversation: Callable[[], None],
    on_close: Callable[[], None],
) -> Dialog:
    """Create Session Actions & Remote Routing dialog with full responsive layout."""
    rem = (current_remote or "off").lower()
    t_tg = "● Telegram (Active)" if rem == "tg" else "○ Route to Telegram"
    t_pan = "● Web Panel (Active)" if rem == "panel" else "○ Route to Web Panel"
    t_both = "● Both (TG + Panel)" if rem == "both" else "○ Route to Both (TG + Panel)"
    t_off = "● Off (Terminal Only)" if rem == "off" else "○ Disable Remote (Local Only)"

    def _do_rem(mode: str):
        on_close()
        on_set_remote(mode)

    buttons = [
        make_clean_button(t_tg, handler=lambda: _do_rem("tg")),
        make_clean_button(t_pan, handler=lambda: _do_rem("panel")),
        make_clean_button(t_both, handler=lambda: _do_rem("both")),
        make_clean_button(t_off, handler=lambda: _do_rem("off")),
        make_clean_button("🔄 Reset Conversation Context", handler=on_reset_conversation),
        make_clean_button("✕ Close Menu", handler=on_close),
    ]

    app = get_app_or_none()
    cols = app.output.get_size()[1] if (app and app.output) else 80
    dialog_width = Dimension(min=36, max=76, preferred=min(68, max(36, cols - 6)))

    return Dialog(
        title="Session Actions & Remote Routing",
        body=HSplit(buttons),
        buttons=[],
        width=dialog_width,
        modal=True,
    )

def create_telegram_connect_dialog(
    current_remote: str = "off",
    on_set_remote: Optional[Callable[[str], None]] = None,
    on_reset_conversation: Optional[Callable[[], None]] = None,
    on_close: Optional[Callable[[], None]] = None,
) -> Dialog:
    """Create Telegram connect alias dialog for backwards compatibility."""
    return create_session_menu_dialog(
        current_remote=current_remote,
        on_set_remote=on_set_remote or (lambda m: None),
        on_reset_conversation=on_reset_conversation or (lambda: None),
        on_close=on_close or (lambda: None),
    )
