# app/api/schemas.py
from typing import List, Optional, Literal, Dict

from pydantic import BaseModel, Field


# ========================================
# Config Sheets / Packet base
# ========================================

class GoogleSheetOutput(BaseModel):
    spreadsheet_id: str = Field(
        ...,
        description="ID de la Google Sheet donde se escribirá el resultado."
    )
    sheet_name: Optional[str] = Field(
        None,
        description="Nombre de la pestaña; opcional si hay un default."
    )


class SheetPosition(BaseModel):
    row: int = Field(
        ...,
        description="Número de fila a editar en la Sheet."
    )
    col_output: int = Field(
        ...,
        description="Número de columna donde se escribirá la URL del PDF final."
    )
    col_status: int = Field(
        ...,
        description="Número de columna donde se escribirá el status / porcentaje."
    )


class ManifestSlot(BaseModel):
    slot: int = Field(
        ...,
        description="Posición entera en el PDF final."
    )
    name: str = Field(
        ...,
        description="Nombre lógico del slot."
    )
    required: bool = Field(
        True,
        description="Indica si el slot es obligatorio."
    )

    # Hint para carpeta y archivo (resolución estilo PacketService)
    folder_hint: Optional[str] = Field(
        None,
        description="Pista de carpeta para resolver el archivo (ej: 'uscis receipts notices', 'vawa').",
    )
    file_hint: Optional[str] = Field(
        None,
        description="Pista de nombre de archivo dentro de la carpeta (ej: 'prima facie renewed', 'pcl carolina').",
    )

    # Legacy / opcional
    filename_patterns: List[str] = Field(
        default_factory=list,
        description="Posibles patrones de archivo (wildcards/regex) para compatibilidad.",
    )
    tags: List[str] = Field(
        default_factory=list,
        description="Etiquetas libres."
    )
    allow_docx: bool = Field(
        False,
        description="Si es True, también se permiten archivos .docx para este slot."
    )


class PacketRequest(BaseModel):
    """
    Request clásico para procesar un paquete (solo PacketService).
    """
    client_name: str = Field(
        ...,
        description="Nombre completo del cliente."
    )
    dropbox_url: str = Field(
        ...,
        description="URL compartida de la carpeta raíz en Dropbox."
    )
    sheet_output_config: Optional[GoogleSheetOutput] = Field(
        None,
        description="Configuración de la Google Sheet donde se escribirá el resultado."
    )
    sheet_position: SheetPosition = Field(
        ...,
        description="Coordenadas a editar en la Sheet."
    )
    manifest: List[ManifestSlot] = Field(
        ...,
        description="Lista ordenada de slots a ensamblar."
    )


class PacketResponse(BaseModel):
    """
    Response para el endpoint que SOLO encola el packet (enqueue).
    """
    status: str
    message: str
    job_id: Optional[str] = None


# ========================================
# OCR Extract (búsqueda por patrón simple)
# ========================================

class OcrRegionConfig(BaseModel):
    """
    Configuración para definir qué área de la página analizar.
    Útil para evitar falsos positivos (ej. leer solo el footer).
    """
    mode: Literal["full_page", "top", "bottom", "custom"] = Field(
        default="full_page",
        description="Modo de recorte: 'full_page' (todo), 'top' (encabezado), 'bottom' (pie de página)."
    )
    height_value: float = Field(
        default=0,
        description=(
            "Altura de la franja. Si es < 1.0 se trata como porcentaje (0.2 = 20%). "
            "Si es >= 1.0 son píxeles."
        )
    )


class OcrExtractRequest(BaseModel):
    """
    Request schema para el endpoint de extracción OCR básica (filtrar páginas)
    cuando YA tienes el PDF en el servidor.
    """
    input_pdf_path: str = Field(
        ...,
        description="Ruta absoluta o relativa del PDF ya descargado en el servidor.",
    )
    pattern: str = Field(
        ...,
        description="Texto o expresión regular a buscar en las páginas del PDF.",
        min_length=1,
    )
    use_regex: bool = Field(
        default=False,
        description="Si es True, 'pattern' se interpreta como expresión regular; si es False, búsqueda literal.",
    )
    region_config: OcrRegionConfig = Field(
        default_factory=OcrRegionConfig,
        description="Configuración opcional para restringir el área de OCR (ej. solo footer)."
    )
    suffix: str = Field(
        default="pattern",
        description="Sufijo para el archivo de salida (ej: 'rapsheet' -> archivo_rapsheet.pdf).",
    )
    case_sensitive: bool = Field(
        default=False,
        description="Si es True, la búsqueda es sensible a mayúsculas/minúsculas.",
    )
    dropbox_folder: str = Field(
        default="/open cases/open cases",
        description="Carpeta de salida en Dropbox para subir el PDF generado (si aplica).",
    )
    ocr_dpi: int = Field(
        default=300,
        description="Resolución DPI para conversión PDF a imagen (mayor calidad = más lento).",
        ge=100,
        le=600,
    )
    ocr_lang: str = Field(
        default="eng",
        description="Código de lenguaje para Tesseract OCR (ej: 'eng', 'spa', 'fra').",
    )


class OcrExtractResponse(BaseModel):
    """
    Response schema para el endpoint de extracción OCR.
    """
    ok: bool = Field(
        ...,
        description="True si la operación fue exitosa, False en caso de error.",
    )
    message: str = Field(
        ...,
        description="Mensaje descriptivo del resultado de la operación.",
    )
    input_pdf_path: str = Field(
        ...,
        description="Ruta del PDF de entrada procesado.",
    )
    output_pdf_path: Optional[str] = Field(
        default=None,
        description="Ruta del PDF generado con las páginas filtradas (null si no hubo coincidencias).",
    )
    matched_pages: List[int] = Field(
        default_factory=list,
        description="Lista de números de página (1-indexed) que coincidieron con el patrón.",
    )


# =====================================================
# OCR Split Exhibits (sólo Dropbox)
# =====================================================

class OcrSplitRequest(BaseModel):
    """
    Request específico para dividir un PDF maestro en múltiples Exhibits,
    resolviendo SIEMPRE el archivo desde Dropbox.

    Se usa en el endpoint: POST /packets/ocr-split-exhibits
    """
    dropbox_url: str = Field(
        ...,
        description=(
            "URL compartida de la carpeta raíz del caso en Dropbox "
            "(ej. la misma que se usa en PacketRequest.dropbox_url)."
        ),
    )
    dropbox_file_pattern: str = Field(
        ...,
        description=(
            "Regex para localizar el PDF maestro dentro de la carpeta del caso. "
            "Ej: '^filed copy\\s*-\\s*.+\\.pdf$'"
        ),
    )
    dropbox_output_subfolder: str = Field(
        default="/9 VAWA/strategy",
        description=(
            "Subcarpeta, relativa a la raíz del caso, donde se guardarán "
            "los Exhibits. Ej: '/9 VAWA/strategy'."
        ),
    )
    exhibit_page_pattern: str = Field(
        default=r"EXHIBIT\s+([A-Z0-9]+)",
        description=(
            "Regex para identificar las páginas separadoras de Exhibit dentro "
            "del PDF maestro. Por defecto: 'EXHIBIT <ID>'."
        ),
    )
    ocr_dpi: int = Field(
        default=300,
        ge=100,
        le=600,
        description="Resolución DPI para OCR del PDF maestro.",
    )
    ocr_lang: str = Field(
        default="eng",
        description="Lenguaje a usar en Tesseract OCR para el split de Exhibits.",
    )


class OcrSplitResponse(BaseModel):
    """
    Response schema para la división de PDF por Exhibits (sólo Dropbox).
    """
    ok: bool = Field(..., description="Estado de la operación.")
    message: str = Field(..., description="Mensaje de resultado.")
    dropbox_url: str = Field(..., description="URL compartida de la carpeta raíz del caso.")
    base_file_path: Optional[str] = Field(
        default=None,
        description="(Opcional) Ruta del PDF maestro resuelto en Dropbox o local.",
    )
    created_files: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Diccionario con los archivos generados. "
            "Key=Identificador (ej. 'EXHIBIT_A'), "
            "Value=Path en Dropbox (o local en fallback)."
        ),
    )


# =====================================================
# Proceso combinado: Exhibits + Packet
# =====================================================

class ExhibitPacketRequest(PacketRequest):
    """
    Request para el endpoint combinado /packets/process-with-exhibits.

    Hereda todos los campos de PacketRequest (client_name, dropbox_url,
    sheet_output_config, sheet_position, manifest) y agrega la configuración
    necesaria para hacer primero el split de Exhibits.
    """
    base_file_pattern: str = Field(
        ...,
        description=(
            "Regex para encontrar el PDF maestro dentro del caso en Dropbox. "
            "Ej: '^filed copy\\s*-\\s*.+\\.pdf$'"
        ),
    )
    exhibit_page_pattern: str = Field(
        default=r"EXHIBIT\s+([A-Z0-9]+)",
        description=(
            "Regex para detectar las páginas separadoras de Exhibit "
            "dentro del PDF maestro."
        ),
    )
    exhibits_output_subfolder: str = Field(
        default="/9 VAWA/strategy",
        description=(
            "Subcarpeta (desde la raíz del caso) donde se guardarán los PDFs "
            "de cada Exhibit generados por el OCR split."
        ),
    )
    ocr_dpi: int = Field(
        default=300,
        ge=100,
        le=600,
        description="Resolución DPI para OCR del PDF maestro en el flujo combinado.",
    )
    ocr_lang: str = Field(
        default="eng",
        description="Lenguaje Tesseract para el OCR del split en el flujo combinado.",
    )


class ExhibitPacketResponse(BaseModel):
    """
    Response para el endpoint combinado /packets/process-with-exhibits.

    Incluye tanto el resultado del split de Exhibits como el resultado del Packet.
    """
    status: str = Field(
        ...,
        description="'ok' si todo salió bien, 'error' si falló el armado del packet.",
    )
    exhibits_created: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Diccionario de Exhibits creados en Dropbox. "
            "Key=Identificador (ej. 'EXHIBIT_A'), Value=Path en Dropbox."
        ),
    )
    packet_output_path: Optional[str] = Field(
        default=None,
        description=(
            "Ruta (en Dropbox o local) del PDF final del packet ensamblado. "
            "En tu implementación actual es la ruta en Dropbox."
        ),
    )
    mask: Optional[str] = Field(
        default=None,
        description="Máscara de presencia de slots en el manifest (si aplica).",
    )
    missing_required: List[int] = Field(
        default_factory=list,
        description="Listado de slots requeridos que no se pudieron resolver.",
    )


# =====================================================
# Proceso Checklist
# =====================================================

class ChecklistSheetPositionIn(BaseModel):
    row: int
    col_check_abuser_spouse: int
    col_check_client_married: int
    col_check_has_children: int
    col_check_has_marriage: int
    col_status: int

class ChecklistExtractRequest(BaseModel):
    client_name: str
    dropbox_url: str
    checklist_file_pattern: Optional[str] = None  # default regex en el service

    sheet_output_config: Optional[dict] = None
    sheet_position: Optional[ChecklistSheetPositionIn] = None

class ChecklistExtractResult(BaseModel):
    abuser_is_spouse: Optional[bool] = None
    client_is_married: Optional[bool] = None
    children_count: Optional[int] = None
    client_marriages_count: Optional[int] = None
    has_children: Optional[bool] = None
    has_marriage: Optional[bool] = None
    case_key: Optional[str] = None
    case_id: Optional[str] = None
    warnings: List[str] = []

class ChecklistExtractResponse(BaseModel):
    ok: bool
    checklist_dropbox_path: Optional[str] = None
    extracted: ChecklistExtractResult

class SmartPacketRequest(ExhibitPacketRequest):
    """
    Request para el proceso inteligente:
    Checklist Analysis -> Filter Manifest -> Split Exhibits -> Merge Packet
    """
    checklist_file_pattern: str = Field(
        default="^CHECKLIST.*\\.pdf$",
        description="Regex para encontrar el PDF del Checklist en la carpeta de Dropbox"
    )

class SmartPacketResponse(ExhibitPacketResponse):
    """
    Response extendido con información de la lógica aplicada.
    """
    case_key_detected: Optional[str] = Field(
        None, description="La clave del caso detectada (ej: '0-1-1-1')"
    )