# app/pdf/text_extractor.py
from __future__ import annotations

from pathlib import Path
from typing import Optional, Callable

from pypdf import PdfReader

from app.logger import get_logger

logger = get_logger(__name__)


class PdfTextExtractor:
    """
    Extrae texto de PDFs nativos (creados desde Word o con capa de texto).
    Diseño simple: hoy solo usa pypdf; mañana puedes inyectar un fallback OCR si lo necesitas.
    """

    def __init__(self, ocr_fallback: Optional[Callable[[str], str]] = None) -> None:
        self.ocr_fallback = ocr_fallback

    def extract_text(self, pdf_path: str) -> str:
        path = Path(pdf_path)
        if not path.exists():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")

        try:
            reader = PdfReader(str(path), strict=False)
            parts: list[str] = []
            for page in reader.pages:
                parts.append(page.extract_text() or "")
            text = "\n".join(parts).strip()

            # Fallback opcional (no lo “activamos” si no lo inyectas)
            if (not text or len(text) < 50) and self.ocr_fallback:
                logger.warning("PDF text seems empty/short; using OCR fallback: %s", pdf_path)
                return (self.ocr_fallback(pdf_path) or "").strip()

            return text
        except Exception as e:
            logger.error("Error extracting text from PDF %s: %s", pdf_path, e)
            # Fallback opcional si falla pypdf
            if self.ocr_fallback:
                logger.warning("Falling back to OCR after extraction error: %s", pdf_path)
                return (self.ocr_fallback(pdf_path) or "").strip()
            raise
