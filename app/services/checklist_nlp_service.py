# app/services/checklist_nlp_service.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any
import re
import shutil

from dropbox.files import FileMetadata

from app.integrations.dropbox_client import DropboxClient
from app.integrations.sheets_client import SheetsClient
from app.integrations.genai_client import GenAiClient
from app.pdf.text_extractor import PdfTextExtractor
from app.services.progress_reporter import ProgressReporter
from app.logger import get_logger

logger = get_logger(__name__)


DEFAULT_CHECKLIST_REGEX = r"^CHECKLIST_.*\.pdf$"


def tri_to_label(v: Optional[bool]) -> str:
    if v is True:
        return "YES"
    if v is False:
        return "NO"
    return "UNKNOWN"


def safe_int_or_none(x: Any, warnings: list[str], field_name: str) -> Optional[int]:
    if x is None:
        return None
    if isinstance(x, int):
        return x
    if isinstance(x, bool):
        return int(x)
    if isinstance(x, str):
        s = x.strip().lower()
        if s in ("no", "ninguno", "ninguna", "none"):
            return 0
        if s.isdigit():
            return int(s)
    warnings.append(f"INVALID_INT:{field_name}:{x}")
    return None


@dataclass
class ChecklistSheetPosition:
    row: int
    col_check_abuser_spouse: int
    col_check_client_married: int
    col_check_has_children: int
    col_check_has_marriage: int
    col_status: int


class ChecklistNlpService:
    """
    Servicio para:
    - encontrar CHECKLIST PDF en Dropbox por regex (default genérico),
    - extraer texto,
    - inferir campos con Vertex (Gemini),
    - escribir 4 columnas YES/NO/UNKNOWN en Google Sheets.
    """

    def __init__(
        self,
        dropbox_client: Optional[DropboxClient] = None,
        sheets_client: Optional[SheetsClient] = None,
        genai_client: Optional[GenAiClient] = None,
        text_extractor: Optional[PdfTextExtractor] = None,
        temp_dir: str = "/tmp",
        checklist_case_map: Optional[Dict[str, str]] = None,
    ) -> None:
        self.dropbox_client = dropbox_client
        self.sheets_client = sheets_client
        self.genai_client = genai_client
        self.text_extractor = text_extractor or PdfTextExtractor()
        self.temp_dir = Path(temp_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.progress = ProgressReporter(sheets_client) if sheets_client else None
        self.checklist_case_map = checklist_case_map or {}

    def process_checklist(
        self,
        *,
        client_name: str,
        dropbox_url: str,
        sheet_output_config: Optional[Any] = None,
        sheet_position: Optional[ChecklistSheetPosition] = None,
        checklist_file_pattern: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self.dropbox_client:
            return self._error("DROPBOX_CLIENT_NOT_AVAILABLE", sheet_output_config, sheet_position)

        if not self.genai_client:
            return self._error("GENAI_CLIENT_NOT_AVAILABLE", sheet_output_config, sheet_position)

        self._report(sheet_output_config, sheet_position, "10% - Buscando CHECKLIST")

        folder_path = self.dropbox_client.resolve_shared_link(dropbox_url)
        if not folder_path:
            return self._error("FAILED_TO_RESOLVE_DROPBOX_LINK", sheet_output_config, sheet_position)

        files_index = self._build_files_index(folder_path)

        regex = checklist_file_pattern or DEFAULT_CHECKLIST_REGEX
        rx = re.compile(regex, re.IGNORECASE)

        matches = self._find_matches(files_index, rx)
        if not matches:
            return self._error("CHECKLIST_NOT_FOUND", sheet_output_config, sheet_position)

        # determinístico
        matches_sorted = sorted(matches)
        checklist_path = matches_sorted[0]
        warnings: list[str] = []
        if len(matches_sorted) > 1:
            warnings.append(f"MULTIPLE_CHECKLIST_MATCHES:{len(matches_sorted)}")

        self._report(sheet_output_config, sheet_position, "30% - Descargando CHECKLIST")

        local_pdf = self._download_to_client_temp(client_name, checklist_path)

        self._report(sheet_output_config, sheet_position, "50% - Extrayendo texto")
        text = self.text_extractor.extract_text(local_pdf)

        self._report(sheet_output_config, sheet_position, "70% - Analizando con Vertex (Gemini)")
        llm = self.genai_client.extract_checklist_fields(text=text)

        # merge warnings
        warnings.extend(llm.get("warnings", []) if isinstance(llm.get("warnings"), list) else ["WARNINGS_NOT_A_LIST"])

        # normalización + derivados
        abuser_is_spouse = llm.get("abuser_is_spouse", None)
        client_is_married = llm.get("client_is_married", None)

        children_count = safe_int_or_none(llm.get("children_count", None), warnings, "children_count")
        client_marriages_count = safe_int_or_none(llm.get("client_marriages_count", None), warnings, "client_marriages_count")

        has_children = None if children_count is None else (children_count >= 1)
        has_marriage = None if client_marriages_count is None else (client_marriages_count >= 1)

        case_key = self._case_key(abuser_is_spouse, client_is_married, has_children, has_marriage)
        case_id = self.checklist_case_map.get(case_key) if case_key else None

        extracted = {
            "abuser_is_spouse": abuser_is_spouse,
            "client_is_married": client_is_married,
            "children_count": children_count,
            "client_marriages_count": client_marriages_count,
            "has_children": has_children,
            "has_marriage": has_marriage,
            "case_key": case_key,
            "case_id": case_id,
            "warnings": warnings,
        }

        # Escribir 4 columnas (YES/NO/UNKNOWN)
        self._write_sheet_4cols(sheet_output_config, sheet_position, extracted)

        self._report(sheet_output_config, sheet_position, "100% - Checklist listo")
        return {"ok": True, "checklist_dropbox_path": checklist_path, "extracted": extracted}

    # ----------------- Helpers -----------------

    def _build_files_index(self, folder_path: str) -> list[str]:
        entries = list(self.dropbox_client.list_folder(folder_path, recursive=True))
        file_paths: list[str] = []
        for entry in entries:
            if isinstance(entry, FileMetadata):
                file_paths.append(entry.path_lower)
        return file_paths

    def _find_matches(self, files_index: list[str], rx: re.Pattern) -> list[str]:
        matches: list[str] = []
        for p in files_index:
            name = p.split("/")[-1]
            if rx.search(name):
                matches.append(p)
        return matches

    def _download_to_client_temp(self, client_name: str, dropbox_path_lower: str) -> str:
        client_folder = self._prepare_client_folder(client_name)
        local_path = self.dropbox_client.download_file(dropbox_path_lower, str(client_folder))
        if not local_path:
            raise RuntimeError(f"Failed to download checklist from Dropbox: {dropbox_path_lower}")
        return local_path

    def _prepare_client_folder(self, client_name: str) -> Path:
        safe_client_name = client_name.replace(" ", "_").replace("/", "_")
        client_folder = self.temp_dir / f"checklist_{safe_client_name}"
        if client_folder.exists():
            shutil.rmtree(client_folder, ignore_errors=True)
        client_folder.mkdir(parents=True, exist_ok=True)
        return client_folder

    def _write_sheet_4cols(self, sheet_output_config, sheet_position: Optional[ChecklistSheetPosition], extracted: Dict[str, Any]) -> None:
        if not self.sheets_client or not sheet_output_config or not sheet_position:
            return

        # --- Lógica de transformación para HUMANOS (Google Sheets) ---
        
        # 1. ¿Quién es el abuser?
        abuser_val = extracted.get("abuser_is_spouse")
        if abuser_val is True:
            abuser_str = "Spouse"
        elif abuser_val is False:
            abuser_str = "Daughter/Son"
        else:
            abuser_str = "Unknown"

        # 2. ¿Cuál es su estado civil?
        married_val = extracted.get("client_is_married")
        if married_val is True:
            married_str = "Married"
        elif married_val is False:
            married_str = "Single"
        else:
            married_str = "Unknown"

        # 3. Cantidad de hijos (Entero o string vacío si es null)
        children_val = extracted.get("children_count")
        children_str = str(children_val) if children_val is not None else "Desconocido"

        # 4. Cantidad de matrimonios (Entero o string vacío si es null)
        marriages_val = extracted.get("client_marriages_count")
        marriages_str = str(marriages_val) if marriages_val is not None else "Desconocido"

        # --- Preparar escritura ---
        
        row = sheet_position.row
        values_by_col = {
            # Columna 1: Descripción del Abuser
            sheet_position.col_check_abuser_spouse: abuser_str,
            # Columna 2: Estado Civil descriptivo
            sheet_position.col_check_client_married: married_str,
            # Columna 3: Número real de hijos
            sheet_position.col_check_has_children: children_str,
            # Columna 4: Número real de matrimonios
            sheet_position.col_check_has_marriage: marriages_str,
        }

        # Escribir en Sheets
        self.sheets_client.write_cells(
            sheet_output_config=sheet_output_config,
            row=row,
            col_values=values_by_col,
        )

    def _case_key(self, a: Optional[bool], b: Optional[bool], c: Optional[bool], d: Optional[bool]) -> Optional[str]:
        vals = [a, b, c, d]
        if any(v is None for v in vals):
            return None
        return "-".join("1" if v else "0" for v in vals)

    def _report(self, sheet_output_config, sheet_position, msg: str) -> None:
        if self.progress and sheet_output_config and sheet_position:
            self.progress.report(sheet_output_config, sheet_position, msg)

    def _error(self, code: str, sheet_output_config, sheet_position) -> Dict[str, Any]:
        self._report(sheet_output_config, sheet_position, f"ERROR - {code}")
        return {
            "ok": False,
            "checklist_dropbox_path": None,
            "extracted": {
                "abuser_is_spouse": None,
                "client_is_married": None,
                "children_count": None,
                "client_marriages_count": None,
                "has_children": None,
                "has_marriage": None,
                "case_key": None,
                "case_id": None,
                "warnings": [code],
            },
        }
