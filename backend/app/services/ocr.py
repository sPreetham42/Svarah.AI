"""OCR cross-check helper (Phase 7): Tesseract on the composed poster strip."""

from __future__ import annotations

import io
import re
import shutil
import subprocess  # nosec - argv-array only
import tempfile
from pathlib import Path

from ..guardian.checks_text import CheckOutcome


def _normalize(text: str) -> str:
    """Conservative OCR-equivalence normalizer."""
    lowered = text.lower()
    lowered = lowered.replace("%", " percent ").replace("–", "-").replace("—", "-")
    lowered = re.sub(r"[^a-z0-9&\- ]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def ocr_fact_zone(png_bytes: bytes, *, expected: list[str]) -> CheckOutcome:
    """Run Tesseract on the lower fact band of the poster.

    Compares OCR output against expected rendered strings with fuzzy matching.
    Indic scripts: Tesseract's default eng model cannot read them, so Indic
    strings are excluded from comparison and the limitation is recorded.
    """
    if shutil.which("tesseract") is None:
        return CheckOutcome(
            "ocr_cross_check",
            "NEEDS_REVIEW",
            0.0,
            {"note": "Tesseract is not installed; OCR skipped and disclosed."},
        )

    try:
        from PIL import Image

        image = Image.open(io.BytesIO(png_bytes)).convert("L")
        # Crop the controlled fact band (bottom 55%) and upscale for OCR.
        width, height = image.size
        band = image.crop((0, int(height * 0.45), width, height))
        band = band.resize((band.width * 2, band.height * 2))
    except Exception as exc:  # noqa: BLE001
        return CheckOutcome("ocr_cross_check", "NEEDS_REVIEW", 0.0, {"note": f"Image load failed: {exc}"})

    with tempfile.TemporaryDirectory(prefix="titan-ocr-") as tmp:
        temp_png = Path(tmp) / "band.png"
        band.save(temp_png, format="PNG")
        try:
            completed = subprocess.run(
                ["tesseract", str(temp_png), "stdout", "--psm", "6"],
                capture_output=True,
                timeout=60,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            return CheckOutcome("ocr_cross_check", "NEEDS_REVIEW", 0.0, {"note": f"OCR unavailable: {exc}"})
        if completed.returncode != 0:
            return CheckOutcome(
                "ocr_cross_check",
                "NEEDS_REVIEW",
                0.0,
                {"note": "Tesseract returned an error; OCR result unusable."},
            )
    ocr_text = completed.stdout.decode("utf-8", "replace")
    ocr_normalized = _normalize(ocr_text)

    matched: list[str] = []
    missing: list[str] = []
    skipped_indic: list[str] = []
    from ..services.poster import _is_indic  # local import avoids a cycle

    for item in expected:
        if not item.strip():
            continue
        if any(_is_indic(ch) for ch in item):
            skipped_indic.append(item)
            continue
        needle = _normalize(item)
        if needle and needle in ocr_normalized:
            matched.append(item)
        else:
            missing.append(item)

    if missing:
        return CheckOutcome(
            "ocr_cross_check",
            "FAIL",
            0.8,
            {"missing": missing, "matched": matched, "ocr_text": ocr_normalized[:400]},
        )
    return CheckOutcome(
        "ocr_cross_check",
        "PASS",
        0.8,
        {"matched": matched, "skipped_indic": skipped_indic},
    )


__all__ = ["ocr_fact_zone"]
