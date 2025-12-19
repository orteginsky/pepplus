# app/services/exhibit_packet_service.py
from __future__ import annotations

from typing import Optional, Dict, Any, List

from app.domain.packet import Packet
from app.domain.manifest import Manifest
from app.domain.slot import Slot
from app.logger import get_logger
from app.services.ocr_extract_service import OcrExtractService
from app.services.packet_service import PacketService
from app.services.checklist_nlp_service import ChecklistNlpService

logger = get_logger(__name__)


# Definición de la Lógica de Negocio (Basada en tus imágenes)
# Key: "AbuserSpouse-Married-HasChildren-HasMarriage" (0/1)
# Value: Configuración de Exhibits requeridos
CASE_LOGIC_MATRIX = {
    # Casos Abuser NO es Spouse (0-X-X-X)
    "0-0-1-0": {"Ex8": ["bonafide"], "Ex9": [], "Ex10": ["H", "M"]},
    "0-0-1-1": {"Ex8": ["bonafide"], "Ex9": ["L"], "Ex10": ["H", "M"]},
    "0-1-1-1": {"Ex8": ["bonafide"], "Ex9": ["K", "L"], "Ex10": ["H", "M"]},
    
    # Casos Abuser SI es Spouse (1-X-X-X)
    "1-0-0-1": {"Ex8": ["gfm"], "Ex9": ["H", "I", "J"], "Ex10": []},
    "1-0-1-1": {"Ex8": ["gfm"], "Ex9": ["H", "I", "J"], "Ex10": ["M"]},
    "1-1-0-1": {"Ex8": ["gfm"], "Ex9": ["H", "I", "J"], "Ex10": []},
    "1-1-1-1": {"Ex8": ["gfm"], "Ex9": ["H", "I", "J"], "Ex10": ["M"]},
}

class ExhibitPacketService:
    """
    Orquesta el flujo completo inteligente:
    1) Analiza Checklist (NLP) -> Obtiene Case Key.
    2) Ajusta el Manifiesto (Exhibits 8, 9, 10) según Case Key.
    3) Hace split del PDF base en Exhibits.
    4) Ensambla el paquete final.
    """

    def __init__(
        self,
        ocr_service: OcrExtractService,
        packet_service: PacketService,
        checklist_service: Optional[ChecklistNlpService] = None,
    ) -> None:
        self.ocr_service = ocr_service
        self.packet_service = packet_service
        self.checklist_service = checklist_service

    def process_smart_packet(
        self,
        packet: Packet,
        checklist_file_pattern: str,
        base_file_pattern: str,
        exhibit_page_pattern: str = r"EXHIBIT\s+([A-Z0-9]+)",
        exhibits_output_subfolder: str = "/9 VAWA/strategy",
    ) -> Dict[str, Any]:
        """
        Proceso "Inteligente":
        1. Busca y lee el Checklist para determinar el tipo de caso (ej: 0-1-1-1).
        2. Filtra el manifiesto original eliminando los exhibits que no aplican.
        3. Ejecuta el proceso normal de Split + Merge.
        """
        if not self.checklist_service:
            raise RuntimeError("ChecklistNlpService is required for smart processing")

        logger.info("Step 0: Analyzing Checklist for Client: %s", packet.client_name)

        # 1. Ejecutar NLP en el Checklist
        checklist_result = self.checklist_service.process_checklist(
            client_name=packet.client_name,
            dropbox_url=packet.dropbox_url,
            checklist_file_pattern=checklist_file_pattern,
            sheet_output_config=None, 
            sheet_position=None
        )

        extracted = checklist_result.get("extracted", {})
        case_key = extracted.get("case_key")
        
        if not case_key:
            logger.warning("Could not determine case_key (Checklist not found or empty). Using FULL manifest.")
            # Podrías decidir abortar aquí si el checklist es obligatorio
        else:
            logger.info("Case Key identified: %s. Adjusting manifest...", case_key)
            # 2. Ajustar Manifiesto
            original_slots = packet.manifest.slots
            filtered_slots = self._filter_manifest_by_case(original_slots, case_key)
            
            # Reemplazamos los slots del paquete con la versión filtrada
            packet.manifest.slots = filtered_slots
            logger.info("Manifest adjusted: %d slots -> %d slots", len(original_slots), len(filtered_slots))

        # 3. Delegar al proceso estándar (Split + Merge)
        return self.process_packet_with_exhibits(
            packet=packet,
            base_file_pattern=base_file_pattern,
            exhibit_page_pattern=exhibit_page_pattern,
            exhibits_output_subfolder=exhibits_output_subfolder
        )

    def process_packet_with_exhibits(
        self,
        packet: Packet,
        base_file_pattern: str,
        exhibit_page_pattern: str = r"EXHIBIT\s+([A-Z0-9]+)",
        exhibits_output_subfolder: str = "/9 VAWA/strategy",
    ) -> Dict[str, Any]:
        logger.info("Starting combined process (split exhibits + packet) for client=%s", packet.client_name)

        # Fase 1: split de exhibits y subida a Dropbox
        created_exhibits = self.ocr_service.split_pdf_by_exhibits(
            dropbox_url=packet.dropbox_url,
            dropbox_file_pattern=base_file_pattern,
            pattern=exhibit_page_pattern,
            dropbox_output_subfolder=exhibits_output_subfolder,
        )

        logger.info("Exhibit split completed. Created %d exhibits.", len(created_exhibits))

        # Fase 2: process_packet con el manifiesto (potencialmente filtrado)
        packet_result = self.packet_service.process_packet(packet)

        return {
            "status": packet_result.get("status"),
            "case_key_detected": getattr(packet, "_temp_case_key", "unknown"), # Informativo
            "exhibits_created": created_exhibits,
            "packet_output_path": packet_result.get("output_path"),
            "mask": packet_result.get("mask"),
            "missing_required": packet_result.get("missing_required", []),
        }

    def _filter_manifest_by_case(self, slots: List[Slot], case_key: str) -> List[Slot]:
        """
        Filtra los slots basándose en la matriz de lógica.
        Solo afecta a Exhibit 8, 9 y 10. Los demás pasan intactos.
        """
        logic = CASE_LOGIC_MATRIX.get(case_key)
        if not logic:
            logger.warning("Case Key '%s' not found in logic matrix. Returning manifest as-is.", case_key)
            return slots

        allowed_ex8 = [tag.lower() for tag in logic.get("Ex8", [])]   # ej: ['bonafide'] o ['gfm']
        allowed_ex9 = [tag.lower() for tag in logic.get("Ex9", [])]   # ej: ['k', 'l']
        allowed_ex10 = [tag.lower() for tag in logic.get("Ex10", [])] # ej: ['h', 'm']

        filtered_slots = []

        for slot in slots:
            name_lower = slot.name.lower()
            tags_lower = [t.lower() for t in slot.meta.tags]

            keep = True

            # --- Lógica de Filtrado ---
            
            # EXHIBIT 8
            if "exhibit 8" in name_lower:
                # Verificamos si alguno de los tags del slot coincide con los permitidos (bonafide o gfm)
                # Nota: Tu JSON usa tag "bonafide". Si la lógica pide "gfm", este slot se borrará.
                # Debes asegurar que tus slots tengan tags distinguibles.
                match = any(req in tags_lower for req in allowed_ex8)
                if not match:
                    keep = False
                    logger.debug("Dropping Ex8 slot %d (tags: %s) for case %s", slot.slot, slot.meta.tags, case_key)

            # EXHIBIT 9
            elif "exhibit 9" in name_lower:
                # Buscamos si el slot corresponde a una letra específica (H, I, J, K, L)
                # Tu JSON usa tags como "exhibit_k", "exhibit_l".
                # La lógica dice ["K", "L"]. Comparamos si "exhibit_k" contiene "k"
                
                # Estrategia: Ver si algún tag permitido está contenido en algún tag del slot
                # Ej: permitido="k", tag_slot="exhibit_k" -> match!
                match = False
                for allowed in allowed_ex9:
                    for tag in tags_lower:
                        if allowed == tag or f"exhibit_{allowed}" == tag:
                            match = True
                            break
                    if match: break
                
                if not match:
                    keep = False
                    logger.debug("Dropping Ex9 slot %d (tags: %s) for case %s", slot.slot, slot.meta.tags, case_key)

            # EXHIBIT 10
            elif "exhibit 10" in name_lower:
                match = False
                for allowed in allowed_ex10:
                    for tag in tags_lower:
                        if allowed == tag or f"exhibit_{allowed}" == tag:
                            match = True
                            break
                    if match: break

                if not match:
                    keep = False
                    logger.debug("Dropping Ex10 slot %d (tags: %s) for case %s", slot.slot, slot.meta.tags, case_key)

            if keep:
                filtered_slots.append(slot)

        return filtered_slots