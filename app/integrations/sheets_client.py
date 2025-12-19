#app/integrations/sheets_client.py
from __future__ import annotations

from typing import Optional, Any

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    service_account = None
    build = None
    HttpError = Exception

from app.domain.packet import SheetOutputConfig, SheetPosition
from app.logger import get_logger

logger = get_logger(__name__)


class SheetsClient:
    """
    Cliente para actualizar celdas en Google Sheets usando la API v4.

    Soporta autenticación con Service Account para operaciones automatizadas.
    """

    def __init__(
        self,
        service=None,
        credentials_path: Optional[str] = None,
        scopes: Optional[list[str]] = None
    ) -> None:
        """
        Args:
            service: Google Sheets API service ya inicializado (para testing/DI)
            credentials_path: Ruta al archivo JSON de service account
            scopes: Scopes de autenticación (default: spreadsheets write)
        """
        if service:
            self.service = service
        elif credentials_path:
            self.service = self._initialize_service(credentials_path, scopes)
        else:
            logger.warning("SheetsClient initialized without credentials, operations will be no-ops")
            self.service = None

    def _initialize_service(self, credentials_path: str, scopes: Optional[list[str]] = None):
        """Inicializa el servicio de Google Sheets API con service account."""
        if not service_account or not build:
            raise ImportError(
                "google-auth and google-api-python-client are required. "
                "Run: pip install google-auth google-api-python-client"
            )

        scopes = scopes or ['https://www.googleapis.com/auth/spreadsheets']

        try:
            logger.info("Initializing Google Sheets API service with credentials: %s", credentials_path)
            credentials = service_account.Credentials.from_service_account_file(
                credentials_path,
                scopes=scopes
            )
            service = build('sheets', 'v4', credentials=credentials)
            logger.info("Google Sheets API service initialized successfully")
            return service

        except Exception as e:
            logger.error("Failed to initialize Google Sheets API service: %s", e)
            return None

    def update_status(self, config: SheetOutputConfig, position: SheetPosition, text: str) -> bool:
        """
        Actualiza la celda de status con el texto proporcionado.

        Args:
            config: Configuración de la Sheet (spreadsheet_id, sheet_name)
            position: Posición de las celdas (row, col_status)
            text: Texto a escribir en la celda de status

        Returns:
            True si exitoso, False en caso de error
        """
        if not self.service:
            logger.warning("SheetsClient service not initialized, skipping update_status")
            return False

        return self._update_cell(
            spreadsheet_id=config.spreadsheet_id,
            sheet_name=config.sheet_name or "Sheet1",
            row=position.row,
            col=position.col_status,
            value=text
        )

    def write_output_url(self, config: SheetOutputConfig, position: SheetPosition, url: str) -> bool:
        """
        Escribe la URL del PDF final en la celda de output.

        Args:
            config: Configuración de la Sheet (spreadsheet_id, sheet_name)
            position: Posición de las celdas (row, col_output)
            url: URL del PDF final

        Returns:
            True si exitoso, False en caso de error
        """
        if not self.service:
            logger.warning("SheetsClient service not initialized, skipping write_output_url")
            return False

        return self._update_cell(
            spreadsheet_id=config.spreadsheet_id,
            sheet_name=config.sheet_name or "Sheet1",
            row=position.row,
            col=position.col_output,
            value=url
        )

    def _update_cell(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        row: int,
        col: int,
        value: str
    ) -> bool:
        """
        Actualiza una celda específica en la hoja.

        Args:
            spreadsheet_id: ID de la spreadsheet
            sheet_name: Nombre de la pestaña
            row: Número de fila (1-indexed)
            col: Número de columna (1-indexed)
            value: Valor a escribir

        Returns:
            True si exitoso, False en caso de error
        """
        try:
            # Convertir número de columna a letra (1=A, 2=B, etc.)
            col_letter = self._col_number_to_letter(col)
            range_name = f"{sheet_name}!{col_letter}{row}"

            body = {
                'values': [[value]]
            }

            logger.debug("Updating cell %s in spreadsheet %s with value: %s", range_name, spreadsheet_id, value)

            result = self.service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=range_name,
                valueInputOption='RAW',
                body=body
            ).execute()

            updated_cells = result.get('updatedCells', 0)
            logger.info("Updated %d cells at %s", updated_cells, range_name)
            return True

        except HttpError as e:
            logger.error("HTTP error updating cell %s!%s%d: %s", sheet_name, col_letter, row, e)
            return False
        except Exception as e:
            logger.error("Unexpected error updating cell: %s", e)
            return False

    def _col_number_to_letter(self, col: int) -> str:
        """
        Convierte número de columna a letra(s).

        Ejemplos:
            1 -> A
            2 -> B
            26 -> Z
            27 -> AA
        """
        result = ""
        while col > 0:
            col -= 1
            result = chr(col % 26 + ord('A')) + result
            col //= 26
        return result

    def batch_update_cells(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        updates: list[tuple[int, int, str]]
    ) -> bool:
        """
        Actualiza múltiples celdas en una sola llamada (más eficiente).

        Args:
            spreadsheet_id: ID de la spreadsheet
            sheet_name: Nombre de la pestaña
            updates: Lista de tuplas (row, col, value)

        Returns:
            True si exitoso, False en caso de error
        """
        if not self.service:
            logger.warning("SheetsClient service not initialized, skipping batch_update_cells")
            return False

        if not updates:
            return True

        try:
            data = []
            for row, col, value in updates:
                col_letter = self._col_number_to_letter(col)
                range_name = f"{sheet_name}!{col_letter}{row}"
                data.append({
                    'range': range_name,
                    'values': [[value]]
                })

            body = {
                'valueInputOption': 'RAW',
                'data': data
            }

            logger.debug("Batch updating %d cells in spreadsheet %s", len(updates), spreadsheet_id)

            result = self.service.spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body=body
            ).execute()

            total_updated = result.get('totalUpdatedCells', 0)
            logger.info("Batch updated %d cells", total_updated)
            return True

        except HttpError as e:
            logger.error("HTTP error in batch update: %s", e)
            return False
        except Exception as e:
            logger.error("Unexpected error in batch update: %s", e)
            return False

    def write_checklist_fields(
        self,
        config: SheetOutputConfig,
        position,
        *,
        abuser_is_spouse: Optional[bool],
        client_is_married: Optional[bool],
        has_children: Optional[bool],
        has_marriage: Optional[bool],
    ) -> bool:
        """
        Escribe 4 campos del checklist en columnas separadas (YES/NO/UNKNOWN).
        position debe tener: row, col_check_abuser_spouse, col_check_client_married,
                            col_check_has_children, col_check_has_marriage
        """
        def tri_to_label(v: Optional[bool]) -> str:
            if v is True:
                return "YES"
            if v is False:
                return "NO"
            return "UNKNOWN"

        if not self.service:
            logger.warning("SheetsClient service not initialized, skipping write_checklist_fields")
            return False

        sheet_name = config.sheet_name or "Sheet1"
        updates = [
            (position.row, position.col_check_abuser_spouse, tri_to_label(abuser_is_spouse)),
            (position.row, position.col_check_client_married, tri_to_label(client_is_married)),
            (position.row, position.col_check_has_children, tri_to_label(has_children)),
            (position.row, position.col_check_has_marriage, tri_to_label(has_marriage)),
        ]
        return self.batch_update_cells(config.spreadsheet_id, sheet_name, updates)
    
    def write_cells(
        self,
        sheet_output_config: SheetOutputConfig,
        row: int,
        col_values: dict[int, str | int | float],
    ) -> bool:
        """
        Escribe valores en múltiples columnas de una misma fila.
        
        Args:
            sheet_output_config: Configuración de la hoja.
            row: Número de fila.
            col_values: Diccionario {indice_columna: valor}.
        """
        if not self.service:
            logger.warning("SheetsClient service not initialized, skipping write_cells")
            return False

        sheet_name = sheet_output_config.sheet_name or "Sheet1"
        
        # Convertir el dict a la lista de tuplas que espera batch_update_cells
        updates = []
        for col, val in col_values.items():
            # Asegurar que el valor sea string para evitar problemas de serialización simple
            updates.append((row, col, str(val)))

        return self.batch_update_cells(sheet_output_config.spreadsheet_id, sheet_name, updates)