# app/services/ocr_extract_service.py
"""
Servicio de orquestación para extracción de páginas basada en OCR.
Coordina: OCR → Filtrado por patrón → Extracción de páginas → Generación de PDF.
"""

import re
from pathlib import Path
from typing import List, Optional,Dict

from app.logger import get_logger
from app.pdf.ocr_engine import OcrEngine, create_ocr_engine
from app.pdf.page_extractor import extract_pages_to_new_pdf, generate_output_path
from app.integrations.dropbox_client import DropboxClient

logger = get_logger(__name__)


class OcrExtractResult:
    """
    Resultado de la operación de extracción OCR.
    """

    def __init__(
        self,
        ok: bool,
        message: str,
        input_pdf_path: str,
        output_pdf_path: Optional[str] = None,
        matched_pages: Optional[List[int]] = None,
        dropbox_path: Optional[str] = None,
    ) -> None:
        self.ok = ok
        self.message = message
        self.input_pdf_path = input_pdf_path
        self.output_pdf_path = output_pdf_path
        self.matched_pages = matched_pages or []
        self.dropbox_path = dropbox_path


class OcrExtractService:
    """
    Servicio que coordina la extracción de páginas de un PDF basándose en
    un patrón de texto detectado mediante OCR.
    """

    def __init__(
        self,
        ocr_engine: Optional[OcrEngine] = None,
        ocr_dpi: int = 300,
        ocr_lang: str = "eng",
        tesseract_cmd: Optional[str] = None,
        dropbox_client: Optional[DropboxClient] = None,
    ) -> None:
        """
        Inicializa el servicio de extracción OCR.

        Args:
            ocr_engine: Instancia de OcrEngine (si no se provee, se crea una por defecto)
            ocr_dpi: DPI para conversión PDF->imagen (usado si no se provee ocr_engine)
            ocr_lang: Lenguaje para tesseract (usado si no se provee ocr_engine)
            tesseract_cmd: Ruta del ejecutable tesseract (usado si no se provee ocr_engine)
        """
        self.ocr_engine = ocr_engine or create_ocr_engine(
            dpi=ocr_dpi,
            lang=ocr_lang,
            tesseract_cmd=tesseract_cmd,
        )
        self.dropbox_client = dropbox_client

    def extract_pages_by_pattern(
        self,
        input_pdf_path: str,
        pattern: str,
        region_config: Optional[dict] = None,
        use_regex: bool = False,
        suffix: str = "pattern",
        case_sensitive: bool = False,
        dropbox_folder: Optional[str] = None,
    ) -> OcrExtractResult:
        """
        Ejecuta la extracción completa:
        1. Aplica OCR al PDF de entrada
        2. Detecta qué páginas contienen el patrón
        3. Extrae esas páginas a un nuevo PDF

        Args:
            input_pdf_path: Ruta del PDF de origen
            pattern: Texto o expresión regular a buscar
            use_regex: Si True, trata 'pattern' como regex; si False, búsqueda literal
            suffix: Sufijo para el archivo de salida (ej: "rapsheet" -> file_rapsheet.pdf)
            case_sensitive: Si True, la búsqueda es case-sensitive

        Returns:
            OcrExtractResult con información del resultado
        """
        logger.info(
            "Starting OCR extraction: input=%s, pattern='%s', use_regex=%s, suffix=%s",
            input_pdf_path,
            pattern,
            use_regex,
            suffix,
        )
        logger.info("Running OCR with region config: %s", region_config)
        # Validar que el archivo existe
        input_file = Path(input_pdf_path)
        if not input_file.exists():
            error_msg = f"Input PDF not found: {input_pdf_path}"
            logger.error(error_msg)
            return OcrExtractResult(
                ok=False,
                message=error_msg,
                input_pdf_path=input_pdf_path,
            )

        try:
            # Paso 1: Extraer texto por página con OCR
            logger.info("Step 1: Running OCR on PDF")
            page_texts = self.ocr_engine.extract_text_by_page(input_pdf_path, region_config)

            if not page_texts:
                error_msg = "OCR extraction returned no pages"
                logger.error(error_msg)
                return OcrExtractResult(
                    ok=False,
                    message=error_msg,
                    input_pdf_path=input_pdf_path,
                )

            logger.info("OCR completed: %d pages processed", len(page_texts))

            # Paso 2: Filtrar páginas que coinciden con el patrón
            logger.info("Step 2: Filtering pages by pattern")
            matched_pages = self._filter_pages_by_pattern(
                page_texts=page_texts,
                pattern=pattern,
                use_regex=use_regex,
                case_sensitive=case_sensitive,
            )

            if not matched_pages:
                warning_msg = f"No pages matched pattern '{pattern}'"
                logger.warning(warning_msg)
                return OcrExtractResult(
                    ok=True,
                    message=warning_msg,
                    input_pdf_path=input_pdf_path,
                    matched_pages=[],
                )

            logger.info("Found %d matching pages: %s", len(matched_pages), matched_pages)

            # Paso 3: Generar ruta de salida
            output_pdf_path = generate_output_path(input_pdf_path, suffix)

            # Paso 4: Extraer páginas al nuevo PDF
            logger.info("Step 3: Extracting matched pages to new PDF")
            extract_pages_to_new_pdf(
                input_pdf_path=input_pdf_path,
                page_numbers=matched_pages,
                output_pdf_path=output_pdf_path,
            )

            dropbox_path = None
            # Paso 5 (opcional): subir a Dropbox si se configuró
            if dropbox_folder and self.dropbox_client:
                try:
                    dropbox_path = self.dropbox_client.upload_file(
                        local_path=output_pdf_path,
                        dropbox_folder=dropbox_folder,
                    )
                    logger.info(
                        "Uploaded extracted PDF to Dropbox folder '%s': %s",
                        dropbox_folder,
                        dropbox_path,
                    )
                    Path(output_pdf_path).unlink(missing_ok=True)

                except Exception as e:
                    logger.error(
                        "Error uploading output PDF to Dropbox folder '%s': %s",
                        dropbox_folder,
                        e,
                    )

            success_msg = (
                f"Successfully extracted {len(matched_pages)} pages matching pattern '{pattern}'"
            )
            logger.info(success_msg)

            return OcrExtractResult(
                ok=True,
                message=success_msg,
                input_pdf_path=input_pdf_path,
                output_pdf_path=output_pdf_path,
                matched_pages=matched_pages,
                dropbox_path=dropbox_path,
            )

        except FileNotFoundError as e:
            error_msg = f"File not found: {str(e)}"
            logger.error(error_msg)
            return OcrExtractResult(
                ok=False,
                message=error_msg,
                input_pdf_path=input_pdf_path,
            )
        except ValueError as e:
            error_msg = f"Validation error: {str(e)}"
            logger.error(error_msg)
            return OcrExtractResult(
                ok=False,
                message=error_msg,
                input_pdf_path=input_pdf_path,
            )
        except Exception as e:
            error_msg = f"Unexpected error during OCR extraction: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return OcrExtractResult(
                ok=False,
                message=error_msg,
                input_pdf_path=input_pdf_path,
            )
    def split_pdf_by_exhibits(
        self,
        dropbox_url: str,
        dropbox_file_pattern: str,
        pattern: str = r"EXHIBIT\s+([A-Z0-9]+)",
        dropbox_output_subfolder: str = "/9 VAWA/strategy",
        temp_dir: str = "/tmp/ocr_exhibits",
    ) -> Dict[str, str]:
        """
        Divide un PDF grande en varios PDFs basados en páginas separadoras "EXHIBIT X",
        resolviendo SIEMPRE el PDF de entrada desde Dropbox usando expresiones regulares.

        Args:
            dropbox_url: URL compartida de la carpeta raíz (igual que en PacketService).
            dropbox_file_pattern: regex para encontrar el PDF base dentro de esa carpeta
                                  (ej: r"filed copy.*carolina alvarez garcia\.pdf").
            pattern: regex para detectar los títulos "EXHIBIT <ID>" dentro del texto OCR.
            dropbox_output_subfolder: subcarpeta (desde la raíz del caso) donde se guardarán
                                      los exhibits. Ej: "/9 VAWA/strategy".
            temp_dir: carpeta temporal local para trabajar los PDFs.

        Returns:
            Dict con {"EXHIBIT_A": "/ruta/en/dropbox/...", ...}
        """
        if not self.dropbox_client:
            raise RuntimeError("DropboxClient is required for split_pdf_by_exhibits")

        # 0. Resolver carpeta raíz del caso a partir del shared link
        root_path = self.dropbox_client.resolve_shared_link(dropbox_url)
        if not root_path:
            raise FileNotFoundError(f"Could not resolve Dropbox folder from URL: {dropbox_url}")

        logger.info("Resolved Dropbox URL to root path: %s", root_path)

        # 1. Buscar el PDF base en todo el árbol del caso usando regex
        matches = self.dropbox_client.find_files_by_pattern(
            root_path=root_path,
            file_pattern=dropbox_file_pattern,
            folder_pattern=None,
            folder_use_regex=True,
            file_use_regex=True,
            only_pdf=True,
        )

        if not matches:
            raise FileNotFoundError(
                f"No Dropbox files matched pattern '{dropbox_file_pattern}' under root '{root_path}'"
            )

        if len(matches) > 1:
            logger.warning(
                "Multiple Dropbox files matched pattern '%s' under '%s'. "
                "Using first match: %s",
                dropbox_file_pattern,
                root_path,
                matches[0],
            )

        remote_pdf_path = matches[0]
        logger.info("Selected Dropbox input file for exhibits split: %s", remote_pdf_path)

        # 2. Descargar PDF base a carpeta temporal local
        tmp_dir_path = Path(temp_dir)
        tmp_dir_path.mkdir(parents=True, exist_ok=True)

        local_input_path = self.dropbox_client.download_file(
            remote_path=remote_pdf_path,
            local_folder=str(tmp_dir_path),
        )

        if not local_input_path:
            raise FileNotFoundError(
                f"Failed to download Dropbox file '{remote_pdf_path}' to '{tmp_dir_path}'"
            )

        input_file = Path(local_input_path)
        logger.info("Downloaded Dropbox file to local path: %s", local_input_path)

        # 3. OCR de todo el PDF
        page_texts = self.ocr_engine.extract_text_by_page(
            str(input_file),
            region_config={"mode": "full_page"},
        )

        # 4. Identificar páginas separadoras de Exhibit
        split_points: Dict[int, str] = {}

        for page_num, text in page_texts.items():
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                identifier = match.group(1).upper()  # A, B, C, etc.
                split_points[page_num] = identifier
                logger.info("Detected split point: Page %d -> EXHIBIT %s", page_num, identifier)

        if not split_points:
            logger.warning("No exhibit patterns found. Returning empty dict.")
            return {}

        sorted_starts = sorted(split_points.keys())
        total_pages = len(page_texts)

        # 5. Calcular carpeta destino de exhibits en Dropbox
        if dropbox_output_subfolder.startswith("/"):
            target_folder = root_path.rstrip("/") + dropbox_output_subfolder
        else:
            target_folder = root_path.rstrip("/") + "/" + dropbox_output_subfolder

        logger.info("Exhibits will be uploaded to Dropbox folder: %s", target_folder)

        created_files: Dict[str, str] = {}

        for i, start_page in enumerate(sorted_starts):
            exhibit_id = split_points[start_page]

            if i + 1 < len(sorted_starts):
                end_page = sorted_starts[i + 1] - 1
            else:
                end_page = total_pages

            pages_to_extract = list(range(start_page, end_page + 1))

            output_name = f"{input_file.stem}_EXHIBIT_{exhibit_id}.pdf"
            output_path = input_file.parent / output_name

            logger.info(
                "Extracting Exhibit %s: Pages %d-%d -> %s",
                exhibit_id,
                start_page,
                end_page,
                output_path,
            )

            extract_pages_to_new_pdf(
                input_pdf_path=str(input_file),
                page_numbers=pages_to_extract,
                output_pdf_path=str(output_path),
            )

            # 6. Subir cada exhibit a Dropbox y limpiar local
            stored_path = str(output_path)
            try:
                remote_exhibit_path = self.dropbox_client.upload_file(
                    local_path=str(output_path),
                    dropbox_folder=target_folder,
                    overwrite=True,
                )
                if remote_exhibit_path:
                    stored_path = remote_exhibit_path
                    try:
                        Path(output_path).unlink(missing_ok=True)
                        logger.info("Local exhibit file deleted after upload: %s", output_path)
                    except Exception as del_err:
                        logger.warning(
                            "Could not delete local exhibit file %s: %s", output_path, del_err
                        )
                else:
                    logger.error(
                        "Upload returned None for Exhibit %s (keeping local path %s)",
                        exhibit_id,
                        output_path,
                    )
            except Exception as e:
                logger.error(
                    "Error uploading Exhibit %s to Dropbox folder '%s': %s",
                    exhibit_id,
                    target_folder,
                    e,
                )

            created_files[f"EXHIBIT_{exhibit_id}"] = stored_path

        # 7. (Opcional) limpiar el PDF base descargado
        try:
            Path(local_input_path).unlink(missing_ok=True)
            logger.info("Local input PDF deleted after split: %s", local_input_path)
        except Exception as del_err:
            logger.warning("Could not delete local input PDF %s: %s", local_input_path, del_err)

        return created_files


    def _filter_pages_by_pattern(
        self,
        page_texts: dict[int, str],
        pattern: str,
        use_regex: bool,
        case_sensitive: bool,
    ) -> List[int]:
        """
        Filtra las páginas que contienen el patrón especificado.

        Args:
            page_texts: Dict con número de página y texto extraído
            pattern: Texto o regex a buscar
            use_regex: Si True, usa regex; si False, búsqueda literal
            case_sensitive: Si True, búsqueda sensible a mayúsculas

        Returns:
            Lista de números de página que coinciden (1-indexed, ordenados)
        """
        matched_pages: List[int] = []

        for page_num, text in page_texts.items():
            if self._text_matches_pattern(text, pattern, use_regex, case_sensitive):
                matched_pages.append(page_num)
                logger.debug("Page %d matched pattern", page_num)

        return sorted(matched_pages)

    def _text_matches_pattern(
        self,
        text: str,
        pattern: str,
        use_regex: bool,
        case_sensitive: bool,
    ) -> bool:
        """
        Determina si un texto contiene el patrón.

        Args:
            text: Texto donde buscar
            pattern: Patrón a buscar
            use_regex: Si True, trata pattern como regex
            case_sensitive: Si True, búsqueda case-sensitive

        Returns:
            True si el texto contiene el patrón
        """
        try:
            if use_regex:
                # Búsqueda con expresión regular
                flags = 0 if case_sensitive else re.IGNORECASE
                return bool(re.search(pattern, text, flags=flags))
            else:
                # Búsqueda literal
                if case_sensitive:
                    return pattern in text
                else:
                    return pattern.lower() in text.lower()
        except re.error as e:
            logger.error("Invalid regex pattern '%s': %s", pattern, e)
            return False
