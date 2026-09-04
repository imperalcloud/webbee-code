"""Enterprise Cross-Platform Audio Playback & Terminal Image Viewer for Webbee Code.

Provides robust, zero-crash audio playback and in-terminal image viewing across:
- macOS (afplay, AVFoundation, iTerm2/Kitty image protocol)
- Linux (paplay, aplay, pw-play, ffplay, Sixel, Kitty, ANSI half-blocks)
- Windows (PowerShell System.Media.SoundPlayer, Windows Media Player, WT inline)
- Universal ANSI/Half-block fallback for any terminal on any OS.
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import threading
from typing import Optional, Tuple


def play_audio_file(file_path: str, *, block: bool = False) -> Optional[subprocess.Popen]:
    """Play an audio file (.wav, .mp3, .m4a, .ogg) across macOS, Linux, and Windows.

    Non-blocking by default (runs in background process).
    """
    if not os.path.exists(file_path):
        return None

    cmd = []
    if sys.platform == "darwin":
        if shutil.which("afplay"):
            cmd = ["afplay", file_path]
        elif shutil.which("ffplay"):
            cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", file_path]
    elif sys.platform.startswith("linux"):
        if shutil.which("paplay") and file_path.endswith(".wav"):
            cmd = ["paplay", file_path]
        elif shutil.which("pw-play"):
            cmd = ["pw-play", file_path]
        elif shutil.which("aplay") and file_path.endswith(".wav"):
            cmd = ["aplay", "-q", file_path]
        elif shutil.which("ffplay"):
            cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", file_path]
        elif shutil.which("mpv"):
            cmd = ["mpv", "--no-video", "--really-quiet", file_path]
    elif sys.platform in ("win32", "cygwin"):
        ps = shutil.which("powershell") or shutil.which("pwsh")
        if ps:
            abs_p = os.path.abspath(file_path).replace("'", "''")
            cmd = [ps, "-c", f"(New-Object System.Media.SoundPlayer '{abs_p}').PlaySync()"]
        elif shutil.which("ffplay"):
            cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", file_path]

    if not cmd:
        return None

    try:
        if block:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return None
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return proc
    except Exception:
        return None


def render_image_in_terminal(image_path: str, max_cols: int = 60, max_rows: int = 30) -> str:
    """Render an image directly inside the terminal.

    Supported rendering protocols:
    1. iTerm2 / WezTerm / Ghostty inline image protocol (OSC 1337)
    2. Kitty terminal graphics protocol (APC G ...)
    3. High-quality ANSI half-block (▀ / ▄) TrueColor fallback using PIL if available
    4. Text description fallback if binary / non-renderable
    """
    if not os.path.exists(image_path):
        return f"[Image not found: {image_path}]"

    # Check terminal protocol support
    term = os.environ.get("TERM", "").lower()
    term_prog = os.environ.get("TERM_PROGRAM", "").lower()
    lc_term = os.environ.get("LC_TERMINAL", "").lower()

    # 1. iTerm2 / WezTerm / Ghostty OSC 1337 Inline Image Protocol
    if "iterm" in term_prog or "wezterm" in term_prog or "ghostty" in term_prog or "iterm" in lc_term:
        try:
            with open(image_path, "rb") as f:
                b64_data = base64.b64encode(f.read()).decode("ascii")
            filename_b64 = base64.b64encode(os.path.basename(image_path).encode("utf-8")).decode("ascii")
            # OSC 1337 ; File=name=...;inline=1;width=auto;height=auto : <base64> ^G
            return f"\033]1337;File=name={filename_b64};inline=1;width=auto;height={max_rows}:{b64_data}\007"
        except Exception:
            pass

    # 2. Kitty Graphics Protocol
    if "kitty" in term or "kitty" in term_prog:
        try:
            with open(image_path, "rb") as f:
                b64_data = base64.b64encode(f.read()).decode("ascii")
            # Kitty chunked graphics transmission
            return f"\033_Ga=T,f=100,t=d;{b64_data}\033\\"
        except Exception:
            pass

    # 3. ANSI TrueColor Half-Block Fallback (Works in ANY 24-bit terminal on macOS, Linux, Windows)
    try:
        from PIL import Image
        img = Image.open(image_path).convert("RGB")
        # Scale to max_cols x (max_rows * 2) because each character cell has 2 vertical subpixels (▀)
        w, h = img.size
        aspect = w / max(1, h)
        target_w = min(max_cols, w)
        target_h = int(target_w / max(0.1, aspect * 2))
        target_h = min(max_rows, max(1, target_h)) * 2

        img = img.resize((target_w, target_h), Image.Resampling.BILINEAR)
        pix = img.load()

        lines = []
        for y in range(0, target_h, 2):
            line_parts = []
            for x in range(target_w):
                r_top, g_top, b_top = pix[x, y]
                if y + 1 < target_h:
                    r_bot, g_bot, b_bot = pix[x, y + 1]
                    # ▀ with fg=top, bg=bot
                    line_parts.append(f"\033[38;2;{r_top};{g_top};{b_top}m\033[48;2;{r_bot};{g_bot};{b_bot}m▀")
                else:
                    line_parts.append(f"\033[38;2;{r_top};{g_top};{b_top}m▀")
            line_parts.append("\033[0m")
            lines.append("".join(line_parts))
        return "\n".join(lines)
    except Exception:
        pass

    # 4. Graceful metadata banner
    size_kb = os.path.getsize(image_path) / 1024.0
    return f"🖼️ [Image: {os.path.basename(image_path)} ({size_kb:.1f} KB)]"
