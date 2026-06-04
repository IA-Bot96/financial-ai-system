"""OCR service — wraps tesseract so the rest of the pipeline stays dependency-free.

Rule-based: the ingest stage decides *when* to OCR; this service just does it.
"""
from __future__ import annotations

import io

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class OCRResult:
    def __init__(self, text: str, confidence: float | None) -> None:
        self.text = text
        self.confidence = confidence


class OCRService:
    """Thin wrapper over pytesseract. Imports are lazy so importing this module
    never fails if tesseract / its python binding is absent."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._configured = False

    def _configure(self) -> None:
        if self._configured:
            return
        import pytesseract

        if self.settings.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = self.settings.tesseract_cmd
        self._configured = True

    def image_to_text(self, image) -> OCRResult:
        """Run OCR on a PIL image, returning text and mean word confidence."""
        import pytesseract

        self._configure()
        lang = self.settings.ocr_lang or "eng"

        text = pytesseract.image_to_string(image, lang=lang)

        confidence: float | None = None
        try:
            data = pytesseract.image_to_data(
                image, lang=lang, output_type=pytesseract.Output.DICT
            )
            confs = [float(c) for c in data.get("conf", []) if c not in ("-1", -1, "")]
            if confs:
                confidence = round(sum(confs) / len(confs), 2)
        except Exception as exc:  # noqa: BLE001
            logger.debug("OCR confidence unavailable: %s", exc)

        return OCRResult(text=text, confidence=confidence)

    def png_bytes_to_text(self, png_bytes: bytes) -> OCRResult:
        from PIL import Image

        with Image.open(io.BytesIO(png_bytes)) as img:
            return self.image_to_text(img)

    def image_to_words(self, image) -> list[dict]:
        """Return OCR words with bounding boxes: {text, x0, x1, top, bottom}.

        Used to reconstruct table grids from scanned pages by clustering words
        into rows and columns.
        """
        import pytesseract

        self._configure()
        lang = self.settings.ocr_lang or "eng"
        data = pytesseract.image_to_data(image, lang=lang, output_type=pytesseract.Output.DICT)

        words: list[dict] = []
        for i, txt in enumerate(data.get("text", [])):
            text = (txt or "").strip()
            if not text:
                continue
            left, top = data["left"][i], data["top"][i]
            words.append(
                {
                    "text": text,
                    "x0": float(left),
                    "x1": float(left + data["width"][i]),
                    "top": float(top),
                    "bottom": float(top + data["height"][i]),
                }
            )
        return words

    def png_bytes_to_words(self, png_bytes: bytes) -> list[dict]:
        from PIL import Image

        with Image.open(io.BytesIO(png_bytes)) as img:
            return self.image_to_words(img)
