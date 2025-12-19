# app/pdf/ocr_engine.py
"""
Módulo de OCR para extracción de texto de PDFs.
Utiliza pdf2image + pytesseract para procesamiento página por página.
"""
from pathlib import Path
from typing import Optional

from pdf2image import convert_from_path
import pytesseract
from PIL import Image

from app.config.settings import settings
from app.logger import get_logger

logger = get_logger(__name__)

class OcrEngine:
    def __init__(
        self,
        dpi: int = 300,
        lang: str = "eng",
        tesseract_cmd: Optional[str] = None,
        poppler_path: str | None = None,
    ) -> None:
        self.dpi = dpi
        self.lang = lang
        self.poppler_path = poppler_path or settings.poppler_path

        if tesseract_cmd or settings.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = (
                tesseract_cmd or settings.tesseract_cmd
            )
        
        logger.debug(f"OcrEngine initialized (dpi={self.dpi}, lang={self.lang})")

    def extract_text_by_page(
        self, 
        pdf_path: str, 
        region_config: Optional[dict] = None
    ) -> dict[int, str]:
        """
        Extrae texto, opcionalmente recortando una región específica antes del OCR.
        
        Args:
            region_config: Dict con keys 'mode' y 'height_value'.
        """
        pdf_file = Path(pdf_path)
        if not pdf_file.exists():
            error_msg = f"PDF file not found: {pdf_path}"
            logger.error(error_msg)
            raise FileNotFoundError(error_msg)

        logger.info(f"Step 1: Converting PDF to images: {pdf_path} (DPI={self.dpi})")

        try:
            # Esta operación puede ser lenta en PDFs grandes
            images = convert_from_path(
                pdf_path,
                dpi=self.dpi,
                fmt="png",
                poppler_path=self.poppler_path,
            )
        except Exception as e:
            logger.error(f"Failed during PDF-to-Image conversion: {e}", exc_info=True)
            raise

        total_pages = len(images)
        logger.info(f"Conversion complete. Starting OCR on {total_pages} pages...")

        page_texts: dict[int, str] = {}
        
        for page_num, image in enumerate(images, start=1):
            # --- LOG DE PROGRESO ---
            # Imprime log en la página 1, cada 5 páginas (5, 10, 15...) y en la última.
            if page_num == 1 or page_num % 5 == 0 or page_num == total_pages:
                logger.info(f"OCR Progress: Processing page {page_num}/{total_pages}...")

            processed_image = self._crop_image(image, region_config)
            
            try:
                text = pytesseract.image_to_string(processed_image, lang=self.lang)
                page_texts[page_num] = text
            except Exception as e:
                logger.error("OCR failed for page %d: %s", page_num, e)
                page_texts[page_num] = ""

        logger.info(f"OCR extraction completed for {pdf_path}. Extracted {len(page_texts)} pages.")
        return page_texts

    def _crop_image(self, image: Image.Image, config: Optional[dict]) -> Image.Image:
        """Aplica recorte vertical basado en la configuración."""
        if not config or config.get("mode") == "full_page":
            return image

        width, height = image.size
        mode = config.get("mode")
        value = config.get("height_value", 0)

        # Convertir porcentaje a píxeles si es necesario (ej: 0.2 -> 20% de altura)
        crop_height = int(value * height) if value < 1.0 else int(value)
        
        # Validar límites
        crop_height = min(crop_height, height)

        if mode == "bottom":
            # (left, top, right, bottom)
            # Recortar solo la franja inferior
            box = (0, height - crop_height, width, height)
            logger.debug("Page region crop: BOTTOM (height=%s px)", crop_height)
            return image.crop(box)
        
        elif mode == "top":
            # Recortar solo la franja superior
            box = (0, 0, width, crop_height)
            logger.debug("Page region crop: TOP (height=%s px)", crop_height)
            return image.crop(box)

        return image

def create_ocr_engine(dpi: int = 300, lang: str = "eng", **kwargs) -> OcrEngine:
    return OcrEngine(dpi=dpi, lang=lang, **kwargs)