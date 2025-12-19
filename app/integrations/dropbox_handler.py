"""
DropboxHandler - Maneja operaciones con la API de Dropbox.
Basado en tu script original con mejoras de manejo de errores.
"""
from __future__ import annotations

import os
import re
from typing import Iterable, Optional

try:
    import dropbox
    from dropbox.exceptions import ApiError, AuthError
    from dropbox.files import WriteMode
except ImportError:
    dropbox = None
    ApiError = Exception
    AuthError = Exception
    WriteMode = None


from app.logger import get_logger

logger = get_logger(__name__)


class DropboxHandler:
    """
    Maneja la conexión y las operaciones básicas con la API de Dropbox.
    Soporta cuentas personales y de equipo.
    """

    def __init__(self, access_token: str):
        """
        Inicializa el cliente de Dropbox.

        Args:
            access_token: Token de acceso de Dropbox

        Raises:
            ValueError: Si el token está vacío
            AuthError: Si el token es inválido
        """
        if not access_token:
            raise ValueError("El token de acceso de Dropbox no puede estar vacío.")

        if dropbox is None:
            raise ImportError("dropbox library is not installed. Run: pip install dropbox")

        try:
            self.dbx = dropbox.Dropbox(access_token)
            cuenta = self.dbx.users_get_current_account()
            logger.info("Conexión exitosa a Dropbox. Usuario: %s", cuenta.name.display_name)

            # Lógica para cuentas de equipo
            if hasattr(cuenta, 'team') and cuenta.team:
                logger.info("Detectada cuenta de Equipo: %s", cuenta.team.name)
                root_namespace_id = cuenta.root_info.root_namespace_id
                logger.info("Cambiando contexto al Espacio de Equipo (namespace: %s)", root_namespace_id)
                self.dbx = self.dbx.with_path_root(dropbox.common.PathRoot.namespace_id(root_namespace_id))

        except AuthError as e:
            logger.error("Error Crítico de Autenticación en Dropbox: %s", e)
            raise
        except Exception as e:
            logger.error("Error Crítico al conectar a Dropbox: %s", e)
            raise

    def get_folder_path_from_shared_link(self, shared_link_url: str) -> Optional[str]:
        """
        Obtiene la ruta de la carpeta desde un link compartido de Dropbox.

        Args:
            shared_link_url: URL del link compartido (ej: https://www.dropbox.com/scl/fo/...)

        Returns:
            Ruta de la carpeta (path_lower) o None si falla
        """
        try:
            logger.debug("Obteniendo metadata para el link: %s", shared_link_url)
            metadata = self.dbx.sharing_get_shared_link_metadata(shared_link_url)

            path = getattr(metadata, 'path_lower', None)

            if path is None:
                logger.error(
                    "El link '%s' no devolvió una ruta ('path_lower' está ausente). "
                    "Verifica el contexto de la cuenta (Personal/Equipo).",
                    shared_link_url
                )
                return None

            logger.info("Ruta obtenida del link: %s", path)
            return path

        except ApiError as e:
            logger.error("No se pudo obtener la ruta para el link '%s': %s", shared_link_url, e)
            return None
        except Exception as e:
            logger.error("Error inesperado obteniendo metadata del link: %s", e)
            return None

    def list_folder_contents(self, folder_path: str, recursive: bool = False) -> list:
        """
        Lista el contenido (archivos y carpetas) de una carpeta específica en Dropbox.

        Args:
            folder_path: Ruta de la carpeta en Dropbox (path_lower)
            recursive: Si es True, lista recursivamente todas las subcarpetas

        Returns:
            Lista de entries (FileMetadata o FolderMetadata)
        """
        try:
            logger.debug("Listando carpeta: %s (recursive=%s)", folder_path, recursive)
            result = self.dbx.files_list_folder(folder_path, recursive=recursive, limit=2000)
            entries = result.entries

            # Manejar paginación
            while result.has_more:
                logger.debug("Paginando... Obteniendo más archivos de %s", folder_path)
                result = self.dbx.files_list_folder_continue(result.cursor)
                entries.extend(result.entries)

            logger.info("Listados %d archivos/carpetas en %s", len(entries), folder_path)
            return entries

        except ApiError as e:
            logger.error("No se pudo listar el contenido de la carpeta '%s': %s", folder_path, e)
            return []
        except Exception as e:
            logger.error("Error inesperado listando carpeta '%s': %s", folder_path, e)
            return []

    def download_file(self, file_path: str, local_folder: str) -> Optional[str]:
        """
        Descarga un archivo de Dropbox a una carpeta local.

        Args:
            file_path: Ruta del archivo en Dropbox (path_lower)
            local_folder: Carpeta local donde guardar el archivo

        Returns:
            Ruta local del archivo descargado o None si falla
        """
        try:
            if not os.path.exists(local_folder):
                os.makedirs(local_folder)

            file_name = os.path.basename(file_path)
            # Sanitizar nombre de archivo para Windows/Mac/Linux
            safe_file_name = re.sub(r'[\\/*?:"<>|]', "_", file_name)
            local_path = os.path.join(local_folder, safe_file_name)

            logger.debug("Descargando: %s -> %s", file_path, local_path)
            self.dbx.files_download_to_file(local_path, file_path)
            logger.info("Archivo descargado: %s", local_path)
            return local_path

        except ApiError as e:
            logger.error("Error al descargar el archivo '%s': %s", file_path, e)
            return None
        except Exception as e:
            logger.error("Error inesperado al guardar el archivo '%s' en '%s': %s", file_path, local_path, e)
            return None

    def get_file_metadata(self, file_path: str):
        """
        Obtiene metadata de un archivo específico.

        Args:
            file_path: Ruta del archivo en Dropbox

        Returns:
            FileMetadata object o None si falla
        """
        try:
            metadata = self.dbx.files_get_metadata(file_path)
            return metadata
        except ApiError as e:
            logger.error("Error obteniendo metadata de '%s': %s", file_path, e)
            return None
    def upload_file(
        self,
        local_path: str,
        dropbox_folder: str,
        overwrite: bool = True,
    ) -> Optional[str]:
        """
        Sube un archivo local a Dropbox.

        Args:
            local_path: Ruta local del archivo (en el servidor).
            dropbox_folder: Carpeta remota destino (ej. '/open cases/open cases/...').
            overwrite: Si True, sobrescribe si ya existe.

        Returns:
            Ruta remota (path_lower/path_display) o None si falla.
        """
        if dropbox is None or WriteMode is None:
            logger.error("Dropbox SDK no está disponible. No se puede subir el archivo.")
            return None

        if not os.path.isfile(local_path):
            logger.error("Local file not found for upload: %s", local_path)
            return None

        # Normalizar carpeta destino
        folder = dropbox_folder.rstrip("/") if dropbox_folder else ""
        if folder and not folder.startswith("/"):
            folder = "/" + folder

        filename = os.path.basename(local_path)
        dropbox_path = f"{folder}/{filename}" if folder else f"/{filename}"

        mode = WriteMode.overwrite if overwrite else WriteMode.add

        logger.debug("Subiendo archivo local '%s' a Dropbox en '%s'", local_path, dropbox_path)

        try:
            with open(local_path, "rb") as f:
                res = self.dbx.files_upload(
                    f.read(),
                    dropbox_path,
                    mode=mode,
                )

            # path_lower es el path canónico en minúsculas
            remote_path = getattr(res, "path_lower", None) or getattr(res, "path_display", dropbox_path)
            logger.info("Archivo subido a Dropbox: %s", remote_path)
            return remote_path

        except ApiError as e:
            logger.error(
                "Error de API al subir archivo a Dropbox (local=%s, remote=%s): %s",
                local_path,
                dropbox_path,
                e,
            )
            return None
        except Exception as e:
            logger.error(
                "Error inesperado al subir archivo a Dropbox (local=%s, remote=%s): %s",
                local_path,
                dropbox_path,
                e,
            )
            return None
    def create_shared_link(self, file_path: str) -> Optional[str]:
        """
        Crea (o recupera) un link compartido para un archivo en Dropbox.

        Args:
            file_path: Ruta del archivo en Dropbox (path_lower), ej: '/open cases/.../packet_CAROLINA.pdf'

        Returns:
            URL del link compartido o None si falla
        """
        try:
            logger.info("Creating shared link for Dropbox file: %s", file_path)
            link_metadata = self.dbx.sharing_create_shared_link_with_settings(file_path)
            url = getattr(link_metadata, "url", None)
            if url:
                logger.info("Shared link created: %s", url)
            else:
                logger.error("Shared link metadata returned without URL for %s", file_path)
            return url

        except ApiError as e:
            # Caso típico: el link ya existe
            try:
                logger.warning(
                    "Could not create shared link (maybe exists) for '%s': %s. Trying to list existing links.",
                    file_path,
                    e,
                )
                links_result = self.dbx.sharing_list_shared_links(path=file_path, direct_only=True)
                if links_result.links:
                    url = links_result.links[0].url
                    logger.info("Reused existing shared link for %s: %s", file_path, url)
                    return url
                else:
                    logger.error("No existing shared links found for %s", file_path)
                    return None
            except Exception as e2:
                logger.error("Error listing existing shared links for %s: %s", file_path, e2)
                return None
        except Exception as e:
            logger.error("Unexpected error creating shared link for %s: %s", file_path, e)
            return None

