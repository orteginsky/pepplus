# app/services/packet_service.py
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional
from uuid import uuid4
from dropbox.files import FileMetadata
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas
from docx2pdf import convert as docx2pdf_convert
import shutil
import re
import sys
import subprocess

from app.domain.manifest import Manifest
from app.domain.packet import Packet
from app.domain.slot_resolution import SlotResolution, SlotResolver
from app.domain.slot import Slot
from app.integrations.dropbox_client import DropboxClient
from app.integrations.enqueuer_client import EnqueuerClient
from app.integrations.sheets_client import SheetsClient
from app.logger import get_logger
from app.pdf.pdf_assembler import merge_pdfs_in_order
from app.services.progress_reporter import ProgressReporter


logger = get_logger(__name__)


class PacketService:
    """
    Coordina la resolución de manifest, descargas, ensamblado y reporte de progreso.
    """

    def __init__(
        self,
        dropbox_client: Optional[DropboxClient] = None,
        sheets_client: Optional[SheetsClient] = None,
        slot_resolver: Optional[SlotResolver] = None,
        enqueuer_client: Optional[EnqueuerClient] = None,
        temp_dir: str = "/tmp",
    ) -> None:
        self.dropbox_client = dropbox_client
        self.sheets_client = sheets_client
        self.slot_resolver = slot_resolver or SlotResolver()
        self.enqueuer_client = enqueuer_client
        self.temp_dir = Path(temp_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.progress = ProgressReporter(sheets_client) if sheets_client else None
        

    def enqueue_packet(self, packet: Packet) -> str:
        """
        Encola un paquete para procesamiento asíncrono a través del servicio enqueuer.

        Returns:
            job_id del trabajo encolado
        """
        if not self.enqueuer_client:
            # Fallback: generar job_id local sin encolar
            job_id = f"job-{uuid4()}"
            logger.warning(
                "EnqueuerClient not available, generated local job_id=%s (not actually enqueued)",
                job_id
            )
            return job_id

        # Serializar packet a dict para enviar al enqueuer
        packet_payload = self._serialize_packet(packet)

        job_id = self.enqueuer_client.enqueue_job(
            service_name="pdf-packet-service",
            endpoint="/api/v1/packets/process",
            payload=packet_payload,
            priority="normal"
        )

        if job_id:
            logger.info("Successfully enqueued packet job_id=%s client=%s", job_id, packet.client_name)
        else:
            # Fallback en caso de error
            job_id = f"job-{uuid4()}"
            logger.error(
                "Failed to enqueue packet, generated fallback job_id=%s client=%s",
                job_id,
                packet.client_name
            )

        return job_id

    def _serialize_packet(self, packet: Packet) -> dict:
        """Serializa un Packet a dict para enviar al enqueuer."""
        return {
            "client_name": packet.client_name,
            "dropbox_url": packet.dropbox_url,
            "sheet_output_config": {
                "spreadsheet_id": packet.sheet_output_config.spreadsheet_id,
                "sheet_name": packet.sheet_output_config.sheet_name,
            } if packet.sheet_output_config else None,
            "sheet_position": {
                "row": packet.sheet_position.row,
                "col_output": packet.sheet_position.col_output,
                "col_status": packet.sheet_position.col_status,
            },
            "manifest": [
                {
                    "slot": slot.slot,
                    "name": slot.name,
                    "required": slot.required,
                    "folder_hint": slot.meta.folder_hint,
                    "file_hint": slot.meta.file_hint,
                    "filename_patterns": slot.meta.filename_patterns,
                    "tags": slot.meta.tags,
                }
                for slot in packet.manifest.slots
            ]
        }

    def process_packet(self, packet: Packet) -> dict:
        """
        Procesa un paquete completo: resuelve slots, descarga archivos, ensambla PDF.

        Returns:
            dict con status, output_path, mask, missing_required
        """
        logger.info("Starting packet processing client=%s", packet.client_name)

        # Reportar progreso: inicio
        if self.progress and packet.sheet_output_config:
            self.progress.report(packet.sheet_output_config, packet.sheet_position, "10% - Resolviendo archivos")

        # Paso 1: Resolver slots
        folder_path = self._get_dropbox_folder_path(packet.dropbox_url)
        if not folder_path:
            return {"status": "error", "message": "Failed to resolve Dropbox shared link"}

        files_index = self._build_files_index(folder_path)
        resolved = self._resolve_slots(packet.manifest, files_index)

        # Verificar slots requeridos faltantes
        missing_required = [
            res.slot.slot for res in resolved
            if res.missing and res.slot.required
        ]

        if missing_required:
            # ⚠️ Ya no abortamos: solo registramos y continuamos
            logger.error("Missing required slots: %s", missing_required)
            if self.progress and packet.sheet_output_config:
                self.progress.report(
                    packet.sheet_output_config,
                    packet.sheet_position,
                    f"ADVERTENCIA - Faltan slots requeridos: {missing_required}"
                )

        # Reportar progreso: descargando
        if self.progress and packet.sheet_output_config:
            self.progress.report(packet.sheet_output_config, packet.sheet_position, "40% - Descargando archivos")

        # Paso 2: Descargar archivos resueltos
        downloaded_paths = self._download_resolved(resolved, packet.client_name)

        # Reportar progreso: ensamblando
        if self.progress and packet.sheet_output_config:
            self.progress.report(packet.sheet_output_config, packet.sheet_position, "70% - Ensamblando PDF")

        # Paso 3: Ensamblar PDF (en disco local /tmp)
        output_path = self._merge(downloaded_paths, packet.client_name)

        # Paso 4: Subir PDF final a Dropbox
        dropbox_output_path: Optional[str] = None
        if output_path and self.dropbox_client:
            try:
                target_folder = folder_path + "/9 VAWA/strategy"
                logger.info(
                    "Uploading final packet PDF for client '%s' to Dropbox folder '%s' from '%s'",
                    packet.client_name,
                    target_folder,
                    output_path,
                )

                dropbox_output_path = self.dropbox_client.upload_file(
                    local_path=str(output_path),
                    dropbox_folder=target_folder,
                    overwrite=True,
                )

                if dropbox_output_path:
                    logger.info("Final packet uploaded to Dropbox: %s", dropbox_output_path)
                    # Opcional: borrar archivo local
                    try:
                        Path(output_path).unlink(missing_ok=True)
                        logger.info("Local packet file deleted after upload: %s", output_path)
                    except Exception as del_err:
                        logger.warning("Could not delete local packet file %s: %s", output_path, del_err)
                else:
                    logger.error(
                        "Dropbox upload returned None for final packet (keeping local path %s)",
                        output_path,
                    )
            except Exception as e:
                logger.error("Error uploading final packet PDF to Dropbox: %s", e)

        # Reportar progreso: completado
        if self.progress and packet.sheet_output_config:
            self.progress.report(packet.sheet_output_config, packet.sheet_position, "100% - Completado")

            # Escribir URL del PDF final: preferir link compartido de Dropbox si existe
            if (output_path or dropbox_output_path) and self.sheets_client:
                url_for_sheet: Optional[str] = None

                if dropbox_output_path and self.dropbox_client:
                    # dropbox_output_path es el path interno en Dropbox (ej: '/open cases/.../packet_x.pdf')
                    shared_url = self.dropbox_client.create_shared_link(dropbox_output_path)
                    # Si no se pudo crear el link, al menos escribir el path interno como fallback
                    url_for_sheet = shared_url or dropbox_output_path
                else:
                    # Fallback total: usar la ruta local del archivo
                    url_for_sheet = str(output_path)

                self.sheets_client.write_output_url(
                    packet.sheet_output_config,
                    packet.sheet_position,
                    url_for_sheet
                )

        logger.info(
            "Finished packet processing client=%s output_local=%s output_dropbox=%s files=%d",
            packet.client_name,
            output_path,
            dropbox_output_path,
            len(downloaded_paths),
        )

        resolved_slot_numbers = [res.slot.slot for res in resolved if not res.missing]
        presence_mask = packet.manifest.presence_mask(resolved_slot_numbers)

        # 🔹 Nuevo: status flexible según si hubo faltantes
        status = "ok_with_missing" if missing_required else "ok"

        return {
            "status": status,
            "output_path": dropbox_output_path or (str(output_path) if output_path else None),
            "mask": presence_mask,
            "missing_required": missing_required,
        }

    def _get_dropbox_folder_path(self, shared_url: str) -> Optional[str]:
        """Resuelve la URL compartida de Dropbox a su path interno."""
        if not self.dropbox_client:
            logger.error("DropboxClient not available")
            return None

        folder_path = self.dropbox_client.resolve_shared_link(shared_url)
        if not folder_path:
            logger.error("Failed to resolve Dropbox shared link: %s", shared_url)
            return None

        logger.info("Resolved Dropbox shared link to: %s", folder_path)
        return folder_path

    def _build_files_index(self, folder_path: str) -> list[str]:
        """Construye un índice de todos los archivos en la carpeta (recursivo)."""
        if not self.dropbox_client:
            logger.error("DropboxClient not available")
            return []

        logger.info("Building files index for: %s", folder_path)
        entries = list(self.dropbox_client.list_folder(folder_path, recursive=True))

        file_paths: list[str] = []
        for entry in entries:
            # Solo archivos
            if isinstance(entry, FileMetadata):
                # path_lower viene en minúsculas → ideal para comparaciones case-insensitive
                file_paths.append(entry.path_lower)

        logger.info(
            "Files index built: %d files (of %d total entries)",
            len(file_paths),
            len(entries),
        )
        return file_paths

    def _resolve_slots(self, manifest: Manifest, files_index: list[str]) -> list[SlotResolution]:
        """Resuelve los slots del manifest contra el índice de archivos."""
        logger.info("Resolving slots count=%d against %d files", len(manifest.slots), len(files_index))
        return self.slot_resolver.resolve(manifest.slots, files_index)

    def _download_resolved(
        self, resolutions: Iterable[SlotResolution], client_name: str
    ) -> list[tuple[Slot, str]]:
        """
        Descarga los archivos resueltos a disco local.

        Returns:
            Lista de tuplas (slot, local_path)
        """
        if not self.dropbox_client:
            logger.error("DropboxClient not available")
            return []

        # Crear / limpiar carpeta temporal para este cliente
        client_folder = self._prepare_client_folder(client_name)

        downloaded: list[tuple[Slot, str]] = []
        for resolution in resolutions:
            if resolution.missing or not resolution.candidate_path:
                logger.debug("Skipping missing slot %d", resolution.slot.slot)
                continue

            local_path = self.dropbox_client.download_file(
                resolution.candidate_path,
                str(client_folder)
            )

            if local_path:
                downloaded.append((resolution.slot, local_path))
                logger.info(
                    "Downloaded slot %d (%s): %s",
                    resolution.slot.slot,
                    resolution.slot.name,
                    local_path,
                )
            else:
                logger.error(
                    "Failed to download slot %d from %s",
                    resolution.slot.slot,
                    resolution.candidate_path,
                )

        logger.info("Downloaded %d/%d files", len(downloaded), len(list(resolutions)))
        return downloaded

    def _merge(self, downloaded: list[tuple[Slot, str]], client_name: str) -> Optional[Path]:
        """
        Ensambla los PDFs descargados en orden de slot, insertando
        una hoja separadora por cada grupo de slots con el mismo name.
        También soporta DOCX convirtiéndolos previamente a PDF.
        """
        if not downloaded:
            logger.warning("No files to merge, skipping PDF assembly")
            return None

        # Ordenar por número de slot
        sorted_files = sorted(downloaded, key=lambda x: x[0].slot)

        final_paths: list[str] = []
        current_group_name: Optional[str] = None

        for slot, original_path in sorted_files:
            group_name = slot.name

            # Si cambia el grupo, insertar hoja separadora
            if group_name != current_group_name:
                current_group_name = group_name
                separator_path = self._create_separator_pdf(
                    title=group_name,
                    client_name=client_name,
                )
                if separator_path:
                    final_paths.append(separator_path)
                    logger.info(
                        "Inserted separator page for group '%s': %s",
                        group_name,
                        separator_path,
                    )

            # Asegurar que el archivo sea PDF (convertir si es DOCX)
            pdf_path = self._ensure_pdf(original_path)
            if not pdf_path:
                logger.error(
                    "Skipping file for slot %d (%s): could not ensure PDF from %s",
                    slot.slot,
                    slot.name,
                    original_path,
                )
                continue

            final_paths.append(pdf_path)

        if not final_paths:
            logger.warning("No files (after grouping) to merge, skipping PDF assembly")
            return None

        safe_client_name = client_name.replace(' ', '_').replace('/', '_')
        output_path = self.temp_dir / f"packet_{safe_client_name}.pdf"

        logger.info("Merging %d PDFs into %s", len(final_paths), output_path)
        merge_pdfs_in_order(final_paths, str(output_path))
        logger.info("Merged PDF written to %s", output_path)

        return output_path

    def _create_separator_pdf(self, title: str, client_name: str) -> Optional[str]:
        """
        Crea un PDF de una sola página con un título grande centrado.

        Devuelve la ruta local del PDF generado.
        """
        try:
            safe_client_name = client_name.replace(' ', '_').replace('/', '_')
            safe_title = re.sub(r"[^a-zA-Z0-9_\-]+", "_", title)
            filename = f"separator_{safe_client_name}_{safe_title}.pdf"
            output_path = self.temp_dir / filename

            c = canvas.Canvas(str(output_path), pagesize=LETTER)
            width, height = LETTER

            # Título grande centrado
            font_name = "Helvetica-Bold"
            font_size = 28
            c.setFont(font_name, font_size)

            text = title.upper()
            text_width = c.stringWidth(text, font_name, font_size)
            x = (width - text_width) / 2.0
            y = height / 2.0

            c.drawString(x, y, text)

            c.showPage()
            c.save()

            return str(output_path)
        except Exception as e:
            logger.error("Error creating separator PDF for '%s': %s", title, e)
            return None
    
    def _ensure_pdf(self, path: str) -> Optional[str]:
        lower = path.lower()
        if lower.endswith(".pdf"):
            return path

        if lower.endswith(".docx"):
            output_path = str(Path(path).with_suffix(".pdf"))

            # Linux: usar LibreOffice
            if sys.platform.startswith("linux"):
                try:
                    logger.info("Converting DOCX to PDF via LibreOffice: %s -> %s", path, output_path)
                    subprocess.run(
                        [
                            "soffice",
                            "--headless",
                            "--convert-to", "pdf",
                            "--outdir", str(Path(path).parent),
                            path,
                        ],
                        check=True,
                    )
                    return output_path
                except subprocess.CalledProcessError as e:
                    logger.error("LibreOffice failed converting %s: %s", path, e)
                    return None

            # Windows / macOS: docx2pdf
            try:
                logger.info("Converting DOCX to PDF via docx2pdf: %s -> %s", path, output_path)
                docx2pdf_convert(path, output_path)
                return output_path
            except Exception as e:
                logger.error("Error converting DOCX to PDF (%s): %s", path, e)
                return None

        logger.warning("Unsupported file type for merge (only pdf/docx): %s", path)
        return None

    def _prepare_client_folder(self, client_name: str) -> Path:
        """
        Prepara la carpeta temporal del cliente:
        - Normaliza el nombre.
        - Si ya existe, la borra completa.
        - La vuelve a crear vacía.

        Así evitamos quedarnos con PDFs DOCX->PDF rotos de corridas anteriores.
        """
        safe_client_name = client_name.replace(' ', '_').replace('/', '_')
        client_folder = self.temp_dir / safe_client_name

        if client_folder.exists():
            try:
                shutil.rmtree(client_folder)
                logger.info("Cleaned previous temp folder for %s: %s", client_name, client_folder)
            except Exception as e:
                logger.error("Could not clean temp folder %s: %s", client_folder, e)

        client_folder.mkdir(parents=True, exist_ok=True)
        return client_folder
