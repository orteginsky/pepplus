# app/api/routes.py
from fastapi import APIRouter, Depends, HTTPException, status
from typing import Optional

from app.api.schemas import (
    PacketRequest,
    PacketResponse,
    OcrExtractRequest,
    OcrExtractResponse,
    OcrSplitRequest,
    OcrSplitResponse,
    ExhibitPacketRequest,
    ExhibitPacketResponse,
    SmartPacketRequest,
    SmartPacketResponse,
    ChecklistExtractRequest, 
    ChecklistExtractResponse
)
from app.config.settings import Settings
from app.domain.manifest import Manifest
from app.domain.packet import Packet, SheetOutputConfig, SheetPosition
from app.domain.slot import Slot, SlotMeta
from app.domain.checklist import ChecklistSheetPosition
from app.integrations.dropbox_client import DropboxClient
from app.integrations.enqueuer_client import EnqueuerClient
from app.integrations.sheets_client import SheetsClient
from app.integrations.genai_client import GenAiClient
from app.logger import get_logger

# Servicios
from app.services.packet_service import PacketService
from app.services.ocr_extract_service import OcrExtractService
from app.services.checklist_nlp_service import ChecklistNlpService
from app.services.exhibit_packet_service import ExhibitPacketService  # <--- Importante

logger = get_logger(__name__)

router = APIRouter(prefix="/packets", tags=["packets"])


# =====================================================
# Dependency Injection Helpers
# =====================================================

def get_settings() -> Settings:
    return Settings()

def get_packet_service(settings: Settings = Depends(get_settings)) -> PacketService:
    """Dependency injection básica para PacketService."""
    # Inicializar enqueuer client si está configurado
    enqueuer_client = None
    if settings.enqueuer_service_url:
        enqueuer_client = EnqueuerClient(service_url=settings.enqueuer_service_url)

    return PacketService(
        dropbox_client=DropboxClient(),
        sheets_client=SheetsClient(credentials_path=settings.google_credentials_path),
        enqueuer_client=enqueuer_client,
        temp_dir=settings.temp_dir,
    )

def get_exhibit_service(
    settings: Settings = Depends(get_settings),
    packet_service: PacketService = Depends(get_packet_service)
) -> ExhibitPacketService:
    """
    Construye el 'Super Servicio' que tiene:
    1. OCR Service (para Split)
    2. Packet Service (para Merge)
    3. Checklist Service (para NLP y Lógica)
    """
    
    # 1. Construir GenAiClient (Requerido para el Checklist)
    if not settings.gcp_project_id:
        logger.warning("GCP Project ID not set. Smart logic might fail.")
    
    genai_client = GenAiClient(
        project_id=settings.gcp_project_id,
        location=getattr(settings, "gcp_location", "us-central1"),
        model=getattr(settings, "genai_model", "gemini-2.5-flash"),
        credentials_path=settings.google_credentials_path 
    )
    
    # 2. Construir Checklist Service
    # Nota: No pasamos sheets_client aquí porque en este flujo 
    # el checklist service solo lee datos, no escribe en la hoja (eso lo hace el packet service al final)
    checklist_service = ChecklistNlpService(
        dropbox_client=DropboxClient(),  # Usa uno nuevo o pasa el del packet_service
        genai_client=genai_client,
        temp_dir=settings.temp_dir,
        checklist_case_map=getattr(settings, "checklist_case_map", {}),
    )

    # 3. Construir OCR Service
    ocr_service = OcrExtractService(
        dropbox_client=DropboxClient(),
        # Podrías parametrizar dpi/lang desde settings si quisieras
    )

    return ExhibitPacketService(
        ocr_service=ocr_service,
        packet_service=packet_service,
        checklist_service=checklist_service
    )


# =====================================================
# Dependency injection
# =====================================================

def get_packet_service() -> PacketService:
    """Dependency injection para PacketService con todas las integraciones."""
    settings = Settings()

    # Inicializar enqueuer client si está configurado
    enqueuer_client = None
    if settings.enqueuer_service_url:
        enqueuer_client = EnqueuerClient(service_url=settings.enqueuer_service_url)

    return PacketService(
        dropbox_client=DropboxClient(),
        sheets_client=SheetsClient(credentials_path=settings.google_credentials_path),
        enqueuer_client=enqueuer_client,
        temp_dir=settings.temp_dir,
    )


# =====================================================
# Endpoint clásico: encolar packet
# =====================================================

@router.post("/enqueue", response_model=PacketResponse, status_code=status.HTTP_202_ACCEPTED)
async def enqueue_packet(
    request: PacketRequest,
    service: PacketService = Depends(get_packet_service),
) -> PacketResponse:
    """
    Encola un paquete de PDFs para procesamiento asíncrono.
    """
    try:
        logger.info("Received enqueue request client=%s", request.client_name)
        packet = build_domain_packet(request)
        job_id = service.enqueue_packet(packet)

        return PacketResponse(
            status="enqueued",
            message=f"Job enqueued successfully for client {packet.client_name}",
            job_id=job_id,
        )

    except ValueError as e:
        logger.error("Validation error in enqueue request: %s", e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid request: {str(e)}",
        )
    except Exception as e:
        logger.error("Unexpected error enqueuing packet: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error while enqueuing packet",
        )


# =====================================================
# Endpoint clásico: procesar sólo packet
# =====================================================

@router.post("/process", response_model=PacketResponse)
async def process_packet(
    request: PacketRequest,
    service: PacketService = Depends(get_packet_service),
) -> PacketResponse:
    """
    Procesa un paquete de PDFs de forma síncrona.
    """
    try:
        logger.info("Received process request client=%s", request.client_name)
        packet = build_domain_packet(request)
        result = service.process_packet(packet)

        # Verificar si hubo errores
        if result.get("status") == "error":
            logger.warning(
                "Packet processing failed for client %s: %s",
                request.client_name,
                result.get("message"),
            )
            return PacketResponse(
                status="error",
                message=result.get("message", "Processing failed"),
                job_id=None,
            )

        # Éxito
        output_path = result.get("output_path")
        missing = result.get("missing_required", [])

        message = f"Processed packet for {packet.client_name}. Output: {output_path}"
        if missing:
            message += f" (missing required slots: {missing})"

        logger.info("Successfully processed packet. Output: %s", output_path)

        return PacketResponse(
            status="completed",
            message=message,
            job_id=None,
        )

    except ValueError as e:
        logger.error("Validation error in process request: %s", e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid request: {str(e)}",
        )
    except FileNotFoundError as e:
        logger.error("File not found during processing: %s", e)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Required file not found: {str(e)}",
        )
    except Exception as e:
        logger.error("Unexpected error processing packet: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error while processing packet",
        )


# =====================================================
# OCR Extract básico (sin Dropbox)
# =====================================================

@router.post("/ocr-extract", response_model=OcrExtractResponse)
async def ocr_extract_pages(request: OcrExtractRequest) -> OcrExtractResponse:
    """
    Extrae páginas de un PDF basándose en un patrón detectado por OCR.
    No usa Dropbox (sólo trabaja con archivos locales).
    """
    try:
        logger.info(
            "Received OCR extract request: input=%s, pattern='%s', regex=%s, region=%s",
            request.input_pdf_path,
            request.pattern,
            request.use_regex,
            request.region_config.mode,
        )

        region_dict = request.region_config.model_dump()

        ocr_service = OcrExtractService(
            ocr_dpi=request.ocr_dpi,
            ocr_lang=request.ocr_lang,
        )

        result = ocr_service.extract_pages_by_pattern(
            input_pdf_path=request.input_pdf_path,
            pattern=request.pattern,
            region_config=region_dict,
            use_regex=request.use_regex,
            suffix=request.suffix,
            case_sensitive=request.case_sensitive,
            dropbox_folder=request.dropbox_folder,
        )

        if not result.ok:
            error_msg = result.message.lower()
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR

            if "not found" in error_msg:
                status_code = status.HTTP_404_NOT_FOUND
            elif "validation" in error_msg or "invalid" in error_msg:
                status_code = status.HTTP_400_BAD_REQUEST

            logger.warning("OCR extraction returned error: %s", result.message)
            raise HTTPException(status_code=status_code, detail=result.message)

        logger.info(
            "OCR extraction completed. Found %d pages. Output: %s",
            len(result.matched_pages),
            result.output_pdf_path,
        )

        return OcrExtractResponse(
            ok=result.ok,
            message=result.message,
            input_pdf_path=result.input_pdf_path,
            output_pdf_path=result.output_pdf_path,
            matched_pages=result.matched_pages,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Unexpected error in OCR extract endpoint: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal error: {str(e)}",
        )


# =====================================================
# OCR Split Exhibits (sólo Dropbox)
# =====================================================

@router.post("/ocr-split-exhibits", response_model=OcrSplitResponse)
async def ocr_split_exhibits(request: OcrSplitRequest) -> OcrSplitResponse:
    """
    Separa un PDF maestro en múltiples PDFs de Exhibits, resolviendo SIEMPRE
    el archivo desde Dropbox usando:
      - dropbox_url (carpeta raíz del caso, link compartido)
      - dropbox_file_pattern (regex para localizar el PDF maestro)
    y subiendo los Exhibits resultantes a 'dropbox_output_subfolder'.
    """
    try:
        logger.info(
            "Received OCR split request (Dropbox mode): url=%s, file_pattern='%s'",
            request.dropbox_url,
            request.dropbox_file_pattern,
        )

        dropbox_client = DropboxClient()

        ocr_service = OcrExtractService(
            ocr_dpi=request.ocr_dpi,
            ocr_lang=request.ocr_lang,
            dropbox_client=dropbox_client,
        )

        # Si no mandan un patrón explícito de página de Exhibit, usar default
        exhibit_pattern = request.exhibit_page_pattern or r"EXHIBIT\s+([A-Z0-9]+)"

        created_files = ocr_service.split_pdf_by_exhibits(
            dropbox_url=request.dropbox_url,
            dropbox_file_pattern=request.dropbox_file_pattern,
            pattern=exhibit_pattern,
            dropbox_output_subfolder=request.dropbox_output_subfolder,
            temp_dir="/tmp/ocr_exhibits",
        )

        logger.info(
            "OCR split completed. Created %d files from pattern '%s'",
            len(created_files),
            request.dropbox_file_pattern,
        )

        return OcrSplitResponse(
            ok=True,
            message=f"Created {len(created_files)} exhibit files.",
            dropbox_url=request.dropbox_url,
            base_file_path=None,
            created_files=created_files,
        )

    except FileNotFoundError as e:
        logger.error("Input file not found for split: %s", e)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Unexpected error splitting PDF exhibits: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error splitting PDF",
        )


# =====================================================
# Orquestador: Split Exhibits + Packet
# =====================================================

@router.post("/process-with-exhibits", response_model=ExhibitPacketResponse)
async def process_packet_with_exhibits(
    request: ExhibitPacketRequest,
    service: PacketService = Depends(get_packet_service),
) -> ExhibitPacketResponse:
    """
    Proceso combinado:
      1) Hace split de Exhibits a partir de un PDF maestro en Dropbox.
      2) Ejecuta process_packet usando el manifest y los archivos ya generados.

    Se apoya en:
      - OcrExtractService.split_pdf_by_exhibits (modo Dropbox)
      - PacketService.process_packet
    """
    try:
        logger.info(
            "Received combined process-with-exhibits request for client=%s",
            request.client_name,
        )

        # Usar el mismo DropboxClient que ya tiene el PacketService
        dropbox_client = service.dropbox_client
        if not dropbox_client:
            logger.error("DropboxClient not available in PacketService")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Dropbox client not available in service.",
            )

        # 1) Split de Exhibits en Dropbox
        ocr_service = OcrExtractService(
            dropbox_client=dropbox_client,
            ocr_dpi=request.ocr_dpi,
            ocr_lang=request.ocr_lang,
        )

        logger.info(
            "Starting Exhibits split for client=%s with base_file_pattern='%s'",
            request.client_name,
            request.base_file_pattern,
        )

        exhibits_created = ocr_service.split_pdf_by_exhibits(
            dropbox_url=request.dropbox_url,
            dropbox_file_pattern=request.base_file_pattern,
            pattern=request.exhibit_page_pattern,
            dropbox_output_subfolder=request.exhibits_output_subfolder,
            temp_dir="/tmp/ocr_exhibits",
        )

        logger.info(
            "Exhibits split completed for client=%s. Created %d exhibits.",
            request.client_name,
            len(exhibits_created),
        )

        if not exhibits_created:
            logger.warning(
                "No exhibits were created for client=%s with base_file_pattern='%s'",
                request.client_name,
                request.base_file_pattern,
            )

        # 2) Construir Packet desde el mismo request (hereda de PacketRequest)
        packet = build_domain_packet(request)

        # 3) Ejecutar PacketService.process_packet
        result = service.process_packet(packet)

        # Si el packet falló, devolvemos el status/error pero manteniendo info de exhibits
        if result.get("status") == "error":
            logger.warning(
                "Packet processing failed after exhibits split for client %s: %s",
                request.client_name,
                result.get("message"),
            )
            return ExhibitPacketResponse(
                status="error",
                exhibits_created=exhibits_created,
                packet_output_path=None,
                mask=None,
                missing_required=result.get("missing_required", []),
            )

        # Éxito total
        packet_output_path = result.get("output_path")
        mask = result.get("mask")
        missing = result.get("missing_required", [])

        logger.info(
            "Combined process-with-exhibits completed for client=%s. "
            "Packet output=%s, missing_required=%s",
            request.client_name,
            packet_output_path,
            missing,
        )

        return ExhibitPacketResponse(
            status="ok",
            exhibits_created=exhibits_created,
            packet_output_path=packet_output_path,
            mask=mask,
            missing_required=missing,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Unexpected error in process-with-exhibits endpoint: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error in combined process-with-exhibits",
        )



# =====================================================
# Orquestador: ChecklistExtractRequest
# =====================================================


@router.post("/checklist-extract", response_model=ChecklistExtractResponse)
async def checklist_extract(
    req: ChecklistExtractRequest,
) -> ChecklistExtractResponse:
    settings = Settings()

    # Construir objetos de dominio
    sheet_cfg: Optional[SheetOutputConfig] = None
    if req.sheet_output_config:
        sheet_cfg = SheetOutputConfig(
            spreadsheet_id=req.sheet_output_config["spreadsheet_id"],
            sheet_name=req.sheet_output_config.get("sheet_name"),
        )

    sheet_pos: Optional[ChecklistSheetPosition] = None
    if req.sheet_position:
        sheet_pos = ChecklistSheetPosition(
            row=req.sheet_position.row,
            col_check_abuser_spouse=req.sheet_position.col_check_abuser_spouse,
            col_check_client_married=req.sheet_position.col_check_client_married,
            col_check_has_children=req.sheet_position.col_check_has_children,
            col_check_has_marriage=req.sheet_position.col_check_has_marriage,
            col_status=req.sheet_position.col_status,
        )

    # Validaciones mínimas (para que falle rápido y claro)
    if not settings.google_credentials_path and sheet_cfg:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="PACKET_GOOGLE_CREDENTIALS_PATH is not configured (required to write to Sheets).",
        )
    if not settings.gcp_project_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="PACKET_GCP_PROJECT_ID is not configured (required for Vertex/Gemini).",
        )

    # Construir clientes
    dropbox_client = DropboxClient()
    sheets_client = SheetsClient(credentials_path=settings.google_credentials_path) if sheet_cfg else None

    # Si todavía no agregas campos a Settings, usa defaults aquí:
    genai_client = GenAiClient(
        project_id=settings.gcp_project_id,
        location=getattr(settings, "gcp_location", "us-central1"),
        model=getattr(settings, "genai_model", "gemini-2.5-flash"),
        credentials_path=settings.google_credentials_path 
    )

    svc = ChecklistNlpService(
        dropbox_client=dropbox_client,
        sheets_client=sheets_client,
        genai_client=genai_client,
        temp_dir=settings.temp_dir,
        checklist_case_map=getattr(settings, "checklist_case_map", {}),
    )

    result = svc.process_checklist(
        client_name=req.client_name,
        dropbox_url=req.dropbox_url,
        checklist_file_pattern=req.checklist_file_pattern,
        sheet_output_config=sheet_cfg,
        sheet_position=sheet_pos,
    )

    return ChecklistExtractResponse(**result)


# =====================================================
# Helper: construir Packet de dominio
# =====================================================

def build_domain_packet(api_request: PacketRequest) -> Packet:
    """
    Convierte un PacketRequest (API schema) o ExhibitPacketRequest
    a un Packet (domain model).
    """
    try:
        # Validar que el manifest no esté vacío
        if not api_request.manifest:
            raise ValueError("Manifest cannot be empty")

        # Construir slots del manifest
        manifest_slots = []
        seen_slot_numbers = set()

        for slot in api_request.manifest:
            # Validar slots duplicados
            if slot.slot in seen_slot_numbers:
                raise ValueError(f"Duplicate slot number: {slot.slot}")
            seen_slot_numbers.add(slot.slot)

            meta = SlotMeta(
                folder_hint=slot.folder_hint,
                file_hint=getattr(slot, "file_hint", None),
                filename_patterns=slot.filename_patterns,
                tags=slot.tags,
                allow_docx=slot.allow_docx,
            )

            manifest_slots.append(
                Slot(
                    slot=slot.slot,
                    name=slot.name,
                    required=slot.required,
                    meta=meta,
                )
            )

        manifest = Manifest(manifest_slots)

        # Construir sheet output config
        sheet_output_config = None
        if api_request.sheet_output_config:
            sheet_output_config = SheetOutputConfig(
                spreadsheet_id=api_request.sheet_output_config.spreadsheet_id,
                sheet_name=api_request.sheet_output_config.sheet_name,
            )

        sheet_position = SheetPosition(
            row=api_request.sheet_position.row,
            col_output=api_request.sheet_position.col_output,
            col_status=api_request.sheet_position.col_status,
        )

        return Packet(
            client_name=api_request.client_name,
            dropbox_url=api_request.dropbox_url,
            manifest=manifest,
            sheet_output_config=sheet_output_config,
            sheet_position=sheet_position,
        )

    except ValueError:
        raise
    except Exception as e:
        logger.error("Error building domain packet: %s", e)
        raise ValueError(f"Failed to build packet: {str(e)}")


# =====================================================
# NUEVO: Endpoint Inteligente (Smart Packet)
# =====================================================

@router.post("/process-smart-packet", response_model=SmartPacketResponse)
async def process_smart_packet(
    request: SmartPacketRequest,
    service: ExhibitPacketService = Depends(get_exhibit_service),
) -> SmartPacketResponse:
    """
    Flujo Completo Inteligente:
    1. Lee Checklist en Dropbox -> Detecta tipo de caso (ej: 0-1-1-1).
    2. Filtra el Manifiesto (elimina Exhibits 8, 9, 10 que no apliquen).
    3. Separa Exhibits del PDF base (Split).
    4. Ensambla el Paquete Final (Merge).
    """
    try:
        logger.info("Received SMART packet request for client=%s", request.client_name)

        # 1. Construir el objeto Packet base
        packet = build_domain_packet(request)

        # 2. Ejecutar la lógica inteligente
        result = service.process_smart_packet(
            packet=packet,
            checklist_file_pattern=request.checklist_file_pattern,
            base_file_pattern=request.base_file_pattern,
            exhibit_page_pattern=request.exhibit_page_pattern,
            exhibits_output_subfolder=request.exhibits_output_subfolder,
        )

        # 3. Manejo de errores
        if result.get("status") == "error":
            logger.warning(
                "Smart processing failed for client %s", request.client_name
            )
            # Aún si falla, intentamos devolver lo que se haya logrado (exhibits creados, etc)
            return SmartPacketResponse(
                status="error",
                case_key_detected=result.get("case_key_detected"),
                exhibits_created=result.get("exhibits_created", {}),
                packet_output_path=None,
                mask=None,
                missing_required=result.get("missing_required", []),
            )

        # 4. Éxito
        return SmartPacketResponse(
            status="ok",
            case_key_detected=result.get("case_key_detected"),
            exhibits_created=result.get("exhibits_created", {}),
            packet_output_path=result.get("packet_output_path"),
            mask=result.get("mask"),
            missing_required=result.get("missing_required", []),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Unexpected error in process-smart-packet: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal server error: {str(e)}",
        )

