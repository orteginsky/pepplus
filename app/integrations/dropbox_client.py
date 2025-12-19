# app/integrations/dropbox/dropbox_client.py
from __future__ import annotations
import re

from typing import Iterable, Optional, List
from dropbox.files import FolderMetadata, FileMetadata

from app.config.settings import Settings
from app.integrations.dropbox_handler import DropboxHandler
from app.integrations.dropbox_token_client import DropboxTokenClient
from app.logger import get_logger

logger = get_logger(__name__)


class DropboxClient:
    """
    Cliente de alto nivel para Dropbox que maneja autenticación automática
    y operaciones comunes de archivos/carpetas.

    Obtiene tokens automáticamente desde el servicio accesstokendropbox
    e inicializa el DropboxHandler.
    """

    def __init__(
        self,
        handler: Optional[DropboxHandler] = None,
        token_client: Optional[DropboxTokenClient] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        """
        Args:
            handler: DropboxHandler ya inicializado (para testing/DI)
            token_client: Cliente de tokens (para testing/DI)
            settings: Settings de la app (para obtener URLs de servicios)
        """
        self.settings = settings or Settings()
        self._handler = handler
        self._token_client = token_client

        # Lazy initialization: solo inicializar handler cuando se necesite
        if self._handler is None and self.settings.dropbox_token_service_url:
            self._initialize_handler()

    def _initialize_handler(self) -> None:
        """Inicializa el handler obteniendo un token fresco del servicio."""
        try:
            if self._token_client is None:
                self._token_client = DropboxTokenClient(
                    service_url=self.settings.dropbox_token_service_url
                )

            # Obtener token usando signature de settings
            token_response = self._token_client.get_token(
                signature=self.settings.dropbox_service_signature,
                service=self.settings.app_name,
            )

            if token_response and token_response.access_token:
                self._handler = DropboxHandler(access_token=token_response.access_token)
                logger.info("DropboxHandler initialized successfully")
            else:
                logger.error("Failed to obtain Dropbox token, handler not initialized")

        except Exception as e:
            logger.error("Error initializing DropboxHandler: %s", e)
            self._handler = None

    @property
    def handler(self) -> Optional[DropboxHandler]:
        """Lazy getter para el handler."""
        if self._handler is None:
            self._initialize_handler()
        return self._handler

    def resolve_shared_link(self, shared_link: str) -> Optional[str]:
        """
        Resuelve un link compartido de Dropbox a su ruta interna.

        Args:
            shared_link: URL del link compartido

        Returns:
            Ruta interna (path_lower) o None si falla
        """
        if not self.handler:
            logger.warning("DropboxHandler not available, cannot resolve link")
            return None
        return self.handler.get_folder_path_from_shared_link(shared_link)

    def list_folder(self, folder_path: str, recursive: bool = False) -> Iterable:
        """
        Lista archivos y carpetas en una ruta de Dropbox.

        Args:
            folder_path: Ruta de la carpeta en Dropbox
            recursive: Si es True, lista recursivamente

        Returns:
            Lista de entries (FileMetadata/FolderMetadata)
        """
        if not self.handler:
            logger.warning("DropboxHandler not available, cannot list folder")
            return []
        return self.handler.list_folder_contents(folder_path, recursive=recursive)

    def download_file(self, remote_path: str, local_folder: str) -> Optional[str]:
        """
        Descarga un archivo de Dropbox.

        Args:
            remote_path: Ruta del archivo en Dropbox
            local_folder: Carpeta local de destino

        Returns:
            Ruta local del archivo descargado o None si falla
        """
        if not self.handler:
            logger.warning("DropboxHandler not available, cannot download file")
            return None
        return self.handler.download_file(remote_path, local_folder)

    # app/integrations/dropbox_client.py

    def upload_file(self, local_path: str, dropbox_folder: str, overwrite: bool = True) -> Optional[str]:
        """
        Sube un archivo local a una carpeta de Dropbox.

        Args:
            local_path: Ruta local del archivo a subir.
            dropbox_folder: Carpeta destino en Dropbox
                            (ej. '/open cases/open cases/cliente/7 Submmisions Materials').
            overwrite: Si True, sobrescribe si ya existe.
        Returns:
            Ruta remota en Dropbox (path_lower) o None si falla.
        """
        if not self.handler:
            logger.warning("DropboxHandler not available, cannot upload file")
            return None

        logger.info(
            "Uploading local file '%s' to Dropbox folder '%s' (overwrite=%s)",
            local_path,
            dropbox_folder,
            overwrite,
        )

        try:
            remote_path = self.handler.upload_file(
                local_path=local_path,
                dropbox_folder=dropbox_folder,
                overwrite=overwrite,
            )
            return remote_path
        except Exception as e:
            logger.error(
                "Error uploading file '%s' to Dropbox folder '%s': %s",
                local_path,
                dropbox_folder,
                e,
            )
            return None


    @staticmethod
    def _normalize_name(name: str) -> str:
        """
        Normaliza nombres de carpeta/archivo:
        - lower()
        - quita dobles espacios
        - reemplaza guiones bajos por espacios
        """
        n = name.lower()
        n = n.replace("_", " ")
        # colapsar espacios múltiples
        n = re.sub(r"\s+", " ", n)
        return n.strip()

    def find_folders_by_pattern(
        self,
        root_path: str,
        pattern: str,
        use_regex: bool = True,
    ) -> List[str]:
        """
        Busca carpetas debajo de root_path cuyo nombre matchee un patrón.

        Args:
            root_path: Carpeta raíz donde empezar a buscar.
            pattern: patrón a buscar (regex o substring normalizado).
            use_regex: si True, se trata como regex sobre el nombre normalizado.

        Returns:
            Lista de paths (path_lower) de carpetas que coinciden.
        """
        if not self.handler:
            logger.warning("DropboxHandler not available, cannot search folders")
            return []

        entries = self.list_folder(root_path, recursive=True)
        norm_pattern = pattern.lower()

        regex = None
        if use_regex:
            # regex sobre nombre normalizado
            regex = re.compile(norm_pattern, re.IGNORECASE)

        matches: List[str] = []

        for entry in entries:
            if isinstance(entry, FolderMetadata):
                norm_name = self._normalize_name(entry.name)
                if use_regex:
                    if regex.search(norm_name):
                        matches.append(entry.path_lower)
                else:
                    if norm_pattern in norm_name:
                        matches.append(entry.path_lower)

        logger.info(
            "find_folders_by_pattern root=%s pattern='%s' -> %d matches",
            root_path,
            pattern,
            len(matches),
        )
        return matches

    def find_files_by_pattern(
        self,
        root_path: str,
        file_pattern: str,
        folder_pattern: Optional[str] = None,
        folder_use_regex: bool = True,
        file_use_regex: bool = True,
        only_pdf: bool = True,
    ) -> List[str]:
        """
        Busca archivos cuyo nombre matchee un patrón, opcionalmente
        restringiendo primero por carpetas que matcheen otro patrón.

        Flujo:
        - Si folder_pattern está definido:
            - buscar carpetas matching bajo root_path
            - buscar archivos solo dentro de esas carpetas
        - Si no:
            - buscar archivos bajo root_path recursivo

        Args:
            root_path: Carpeta raíz de búsqueda.
            file_pattern: patrón para el nombre de archivo.
            folder_pattern: patrón para nombre de carpeta (opcional).
            folder_use_regex: si True, patrón de carpeta como regex.
            file_use_regex: si True, patrón de archivo como regex.
            only_pdf: si True, filtrar a .pdf

        Returns:
            Lista de paths (path_lower) de archivos matching.
        """
        if not self.handler:
            logger.warning("DropboxHandler not available, cannot search files")
            return []

        entries = list(self.list_folder(root_path, recursive=True))

        # 1) Si hay patrón de carpeta, filtrar primero carpetas candidatas
        candidate_folder_paths: Optional[List[str]] = None
        if folder_pattern:
            candidate_folder_paths = self.find_folders_by_pattern(
                root_path=root_path,
                pattern=folder_pattern,
                use_regex=folder_use_regex,
            )

        # Precompilar patrón de archivos
        norm_file_pattern = file_pattern.lower()
        file_regex = None
        if file_use_regex:
            file_regex = re.compile(norm_file_pattern, re.IGNORECASE)

        def file_matches(entry: FileMetadata) -> bool:
            name_norm = self._normalize_name(entry.name)
            if only_pdf and not name_norm.endswith(".pdf"):
                return False
            if file_use_regex:
                return bool(file_regex.search(name_norm))
            else:
                return norm_file_pattern in name_norm

        matches: List[str] = []

        for entry in entries:
            if not isinstance(entry, FileMetadata):
                continue

            # Restringir por carpeta si aplica
            if candidate_folder_paths:
                # path_lower inicia con una de las carpetas candidatas
                if not any(entry.path_lower.startswith(folder) for folder in candidate_folder_paths):
                    continue

            if file_matches(entry):
                matches.append(entry.path_lower)

        logger.info(
            "find_files_by_pattern root=%s folder_pattern='%s' file_pattern='%s' -> %d matches",
            root_path,
            folder_pattern,
            file_pattern,
            len(matches),
        )
        return matches
    def create_shared_link(self, remote_path: str) -> Optional[str]:
        """
        Crea o recupera un link compartido para un archivo en Dropbox.

        Args:
            remote_path: Ruta del archivo en Dropbox (path_lower).

        Returns:
            URL del link compartido o None si falla.
        """
        if not self.handler:
            logger.warning("DropboxHandler not available, cannot create shared link")
            return None
        return self.handler.create_shared_link(remote_path)
