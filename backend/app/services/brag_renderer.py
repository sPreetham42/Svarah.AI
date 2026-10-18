"""Frame renderer for the Brag Director's storyboard (poster + campaign video).

One HTML template (`brag_template.html`) draws both deliverables in headless
Chrome. Following the brag-slim method, the video is a pure function of time:
`render(t)` lays out frame *t*, a screenshot is taken, and the frames are
piped to FFmpeg — so the result is identical on every run and never depends
on wall-clock animation.

Facts on screen come only from the locked token map. The template tags every
string it draws (`FACT` = a token value verbatim, `HEADLINE`, `BUSINESS`,
`COPY` = digit-free framing copy from the agent); those tags are read back
from the page and returned as the rendered-facts registry Guardian verifies.

Needs the `playwright` package and an installed Chrome or Edge; without them
`renderer_available()` is False and callers fall back or report it.
"""

from __future__ import annotations

import array
import asyncio
import base64
import functools
import io
import json
import math
import subprocess  # nosec - argv arrays only
import tempfile
import wave
from pathlib import Path
from typing import Any

from ..config import get_settings
from ..errors import ValidationError
from .fact_engine import substitute_tokens

TEMPLATE = Path(__file__).with_name("brag_template.html")
POSTER_SIZE = (1080, 1350)
VIDEO_SIZE = (720, 1280)
FPS = 24
#: Pages rendering frames side by side.
WORKERS = 4
_CHANNELS = ("chrome", "msedge")
FACT_NAMES = ("PRODUCT", "DISCOUNT", "PRICE", "DAYS", "WINDOW", "AUDIENCE", "LOCATION", "CONDITIONS")

#: Seconds per scene before stretching to the voice-over.
SCENE_SECONDS = {"hook": 2.6, "reveal": 3.0, "offer": 3.2, "when": 3.4, "outro": 3.2}
MAX_VIDEO_SECONDS = 22.0


class RendererUnavailable(ValidationError):
    code = "renderer_unavailable"


@functools.lru_cache(maxsize=1)
def renderer_available() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


# ── colour helpers: the agent picks colours, code guarantees legibility ──
def _rgb(value: str) -> tuple[int, int, int]:
    return tuple(int(value[i : i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


def _luminance(value: str) -> float:
    def channel(c: int) -> float:
        x = c / 255
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in _rgb(value))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _ink_for(background: str) -> str:
    return "#14110d" if _contrast(background, "#14110d") >= _contrast(background, "#ffffff") else "#ffffff"


def _art_data_uri(art: bytes | None, width: int) -> str | None:
    if not art:
        return None
    from PIL import Image

    image = Image.open(io.BytesIO(art)).convert("RGB")
    if image.width > width:
        image = image.resize((width, round(image.height * width / image.width)))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=86)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def build_spec(
    board: dict[str, Any],
    tokens: dict[str, str],
    *,
    mode: str,
    business_name: str,
    headline: str,
    art: bytes | None,
    voice_seconds: float | None = None,
) -> dict[str, Any]:
    """Everything the template needs, with the agent's copy token-substituted."""
    width, height = POSTER_SIZE if mode == "poster" else VIDEO_SIZE
    bg, accent = board["palette"]["bg"], board["palette"]["accent"]
    if _contrast(bg, accent) < 2.6:  # an accent that vanishes into the background
        accent = "#ffc53d" if _luminance(bg) < 0.4 else "#c2341c"

    def text(group: str, key: str) -> str:
        return substitute_tokens(board[group][key], tokens)

    facts = {name: tokens[name] for name in FACT_NAMES if tokens.get(name)}
    discount = facts.get("DISCOUNT", "")
    spec: dict[str, Any] = {
        "mode": mode,
        "W": width,
        "H": height,
        "style": board["style"],
        "motion": board["motion"],
        "bg": bg,
        "accent": accent,
        "ink": _ink_for(bg),
        "accentInk": _ink_for(accent),
        "art": _art_data_uri(art, width),
        "business": business_name,
        "headline": headline,
        "facts": facts,
        "discountNeedsOff": bool(discount) and not discount.rstrip().lower().endswith("off"),
        "poster": {"layout": board["poster"]["layout"], "kicker": text("poster", "kicker"), "cta": text("poster", "cta")},
    }
    if mode == "video":
        names = ["hook", "reveal", *board["video"]["order"], "outro"]
        if not (facts.get("DAYS") or facts.get("WINDOW")):
            names.remove("when")
        base = sum(SCENE_SECONDS[n] for n in names)
        # Stretch every scene evenly so the voice-over finishes before the end.
        target = min(MAX_VIDEO_SECONDS, max(base, (voice_seconds or 0) + 1.0))
        scale = target / base
        spec["video"] = {
            **{key: text("video", key) for key in ("hook", "reveal_kicker", "offer_caption", "when_caption", "cta")},
            "timeline": [[n, round(SCENE_SECONDS[n] * scale, 3)] for n in names],
        }
    return spec


def _html(spec: dict[str, Any]) -> str:
    payload = json.dumps(spec, ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.read_text(encoding="utf-8").replace("__SPEC__", payload)


async def _launch(playwright):
    last: Exception | None = None
    for channel in _CHANNELS:
        try:
            return await playwright.chromium.launch(channel=channel, headless=True)
        except Exception as exc:  # noqa: BLE001 - try the next installed browser
            last = exc
    try:
        return await playwright.chromium.launch(headless=True)  # bundled Chromium, if installed
    except Exception as exc:  # noqa: BLE001
        raise RendererUnavailable(
            "No Chrome, Edge or Playwright Chromium is installed for rendering.",
            details={"error": str(last or exc)[:300]},
        ) from exc


async def _open(browser, html: str, width: int, height: int):
    page = await browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
    await page.set_content(html, wait_until="load")
    await page.evaluate("window.ready")
    return page


async def _render_poster(spec: dict[str, Any]) -> tuple[bytes, list[dict[str, Any]]]:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await _launch(playwright)
        try:
            page = await _open(browser, _html(spec), *POSTER_SIZE)
            rendered = await page.evaluate("window.rendered()")
            png = await page.screenshot(type="png")
        finally:
            await browser.close()
    return png, rendered


async def _render_frames(spec: dict[str, Any], ffmpeg_stdin) -> tuple[float, list[dict[str, Any]], int]:
    from playwright.async_api import async_playwright

    html = _html(spec)
    async with async_playwright() as playwright:
        browser = await _launch(playwright)
        try:
            pages = await asyncio.gather(*(_open(browser, html, *VIDEO_SIZE) for _ in range(WORKERS)))
            sessions = [await page.context.new_cdp_session(page) for page in pages]
            duration = float(await pages[0].evaluate("window.DURATION"))
            rendered = await pages[0].evaluate("window.rendered()")
            total = round(duration * FPS)

            async def shot(worker: int, index: int) -> bytes:
                await pages[worker].evaluate("t => window.render(t)", index / FPS)
                result = await sessions[worker].send(
                    "Page.captureScreenshot",
                    {"format": "jpeg", "quality": 84, "optimizeForSpeed": True},
                )
                return base64.b64decode(result["data"])

            for start in range(0, total, WORKERS):
                batch = range(start, min(total, start + WORKERS))
                frames = await asyncio.gather(*(shot(w, i) for w, i in enumerate(batch)))
                for frame in frames:  # frames must reach FFmpeg in order
                    ffmpeg_stdin.write(frame)
        finally:
            await browser.close()
    return duration, rendered, total


def render_poster(spec: dict[str, Any]) -> tuple[bytes, list[dict[str, Any]]]:
    if not renderer_available():
        raise RendererUnavailable("The frame renderer is not installed (pip install playwright).")
    from .async_bridge import run_sync

    return run_sync(_render_poster(spec))


def wav_seconds(data: bytes | None) -> float | None:
    if not data:
        return None
    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            return reader.getnframes() / float(reader.getframerate())
    except (wave.Error, EOFError):
        return None


def music_bed() -> Path:
    """A soft instrumental bed (D–Bm–G–A, 100 BPM), synthesised once and cached."""
    path = Path(get_settings().assets_dir) / "_cache" / "brag_bed_v1.wav"
    if path.is_file():
        return path
    rate, seconds, beat = 22050, 24.0, 0.6
    total = int(rate * seconds)
    samples = [0.0] * total
    freq = lambda midi: 440.0 * 2 ** ((midi - 69) / 12)  # noqa: E731
    chords = [(50, 57, 62, 66), (47, 54, 59, 62), (43, 50, 55, 59), (45, 52, 57, 61)]
    two_pi = 2 * math.pi
    for bar in range(int(seconds / 2.4)):
        chord = chords[bar % 4]
        start = int(bar * 2.4 * rate)
        length = int(2.4 * rate)
        for note in chord:  # pad
            step = two_pi * freq(note) / rate
            for i in range(length):
                if start + i >= total:
                    break
                env = min(1.0, i / (0.5 * rate)) * min(1.0, (length - i) / (0.4 * rate))
                samples[start + i] += 0.045 * env * (math.sin(step * i) + 0.3 * math.sin(2 * step * i))
        for k, pick in enumerate((2, 3, 1, 3, 2, 3, 1, 2)):  # plucked eighths
            step = two_pi * freq(chord[pick] + 12) / rate
            at = start + int(k * 0.3 * rate)
            for i in range(int(0.5 * rate)):
                if at + i >= total:
                    break
                samples[at + i] += 0.07 * math.exp(-i / (0.11 * rate)) * math.sin(step * i)
        for b in range(4):  # soft kick
            at = start + int(b * beat * rate)
            phase = 0.0
            for i in range(int(0.22 * rate)):
                if at + i >= total:
                    break
                phase += two_pi * (48 + 90 * math.exp(-i / (0.02 * rate))) / rate
                samples[at + i] += 0.22 * math.exp(-i / (0.09 * rate)) * math.sin(phase)
    peak = max(abs(s) for s in samples) or 1.0
    pcm = array.array("h", (int(32767 * 0.8 * s / peak) for s in samples))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(pcm.tobytes())
    return path


def render_video(spec: dict[str, Any], *, voice_wav: bytes | None) -> tuple[bytes, dict[str, Any]]:
    """Render the storyboard to an MP4 (H.264 + AAC). Returns (bytes, info)."""
    if not renderer_available():
        raise RendererUnavailable("The frame renderer is not installed (pip install playwright).")
    from .async_bridge import run_sync
    from .video_service import _ffmpeg_binary

    ffmpeg = _ffmpeg_binary()
    total_seconds = sum(d for _, d in spec["video"]["timeline"])
    bed = music_bed()
    with tempfile.TemporaryDirectory(prefix="titan-brag-") as tmp:
        out = Path(tmp) / "brag.mp4"
        args = [ffmpeg, "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(FPS), "-c:v", "mjpeg", "-i", "-"]
        fade = f"afade=t=out:st={max(0.0, total_seconds - 1.2):.2f}:d=1.2"
        if voice_wav:
            voice_path = Path(tmp) / "voice.wav"
            voice_path.write_bytes(voice_wav)
            args += ["-i", str(voice_path), "-i", str(bed)]
            # Voice up front, music tucked underneath it.
            mix = f"[1:a]adelay=450|450,volume=1.25[v];[2:a]volume=0.16[m];[v][m]amix=inputs=2:duration=longest:normalize=0,{fade}[a]"
        else:
            args += ["-i", str(bed)]
            mix = f"[1:a]volume=0.6,{fade}[a]"
        args += [
            "-filter_complex", mix, "-map", "0:v", "-map", "[a]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
            "-r", str(FPS), "-c:a", "aac", "-b:a", "128k", "-t", f"{total_seconds:.3f}",
            "-movflags", "+faststart", str(out),
        ]
        process = subprocess.Popen(args, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            duration, rendered, frames = run_sync(_render_frames(spec, process.stdin))
        except BaseException:
            process.kill()
            raise
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass
        stderr = process.stderr.read().decode("utf-8", "replace")
        if process.wait(timeout=120) != 0 or not out.is_file():
            raise ValidationError("FFmpeg could not encode the video.", code="ffmpeg_failed", details={"stderr": stderr[-400:]})
        data = out.read_bytes()
    return data, {"seconds": round(duration, 2), "frames": frames, "fps": FPS, "rendered": rendered, "has_voice": bool(voice_wav)}
