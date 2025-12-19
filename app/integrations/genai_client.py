# app/integrations/genai_client.py
from __future__ import annotations

import json
import os

from typing import Any, Dict, Optional

from google import genai
from google.oauth2 import service_account
from google import genai

from app.logger import get_logger

logger = get_logger(__name__)

VERTEX_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

CHECKLIST_SCHEMA: Dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "abuser_is_spouse": {
            "type": "BOOLEAN",
            "nullable": True
        },
        "client_is_married": {
            "type": "BOOLEAN",
            "nullable": True
        },
        "children_count": {
            "type": "INTEGER",
            "nullable": True
        },
        "client_marriages_count": {
            "type": "INTEGER",
            "nullable": True
        },
        "warnings": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
            "nullable": True
        },
    },
    "required": [
        "abuser_is_spouse",
        "client_is_married",
        "children_count",
        "client_marriages_count",
        "warnings",
    ],
    "propertyOrdering": [
        "abuser_is_spouse",
        "client_is_married",
        "children_count",
        "client_marriages_count",
        "warnings"
    ]
}


class GenAiClient:
    """
    Adapter ligero para Gemini en Vertex AI usando google-genai.

    - Aísla dependencias del SDK.
    - Obliga structured output (JSON) usando response_schema.
    """

    def __init__(
        self,
        project_id: str,
        location: str = "us-central1",
        model: str = "gemini-2.5-flash",
        credentials_path: Optional[str] = None,
    ) -> None:
        self.project_id = project_id
        self.location = location
        self.model = model

        # Cargar credenciales explícitas si existen
        creds = None
        if credentials_path:
            try:
                logger.info(f"Cargando credenciales Vertex desde: {credentials_path}")
                # 1. Cargar el archivo
                creds_source = service_account.Credentials.from_service_account_file(credentials_path)
                # 2. IMPORTANTE: Asignar los scopes explícitamente
                creds = creds_source.with_scopes(VERTEX_SCOPES)
            except Exception as e:
                logger.error(f"Error cargando credenciales desde {credentials_path}: {e}")
                # Fallback: dejar que ADC intente encontrarlo si falla la carga explícita

        # Inicializar cliente pasando las credenciales
        self.client = genai.Client(
            vertexai=True, 
            project=project_id, 
            location=location,
            credentials=creds
        )
    
    def extract_checklist_fields(self, *, text: str) -> Dict[str, Any]:
        """
        Devuelve dict con:
          - abuser_is_spouse: bool|null
          - client_is_married: bool|null
          - children_count: int|null
          - client_marriages_count: int|null
          - warnings: list[str]
        """
        prompt = self._build_prompt(text)

        try:
            resp = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": CHECKLIST_SCHEMA,
                },
            )

            raw = (resp.text or "").strip()
            if not raw:
                return {
                    "abuser_is_spouse": None,
                    "client_is_married": None,
                    "children_count": None,
                    "client_marriages_count": None,
                    "warnings": ["EMPTY_MODEL_RESPONSE"],
                }

            return json.loads(raw)

        except Exception as e:
            logger.error("GenAI extraction error: %s", e)
            return {
                "abuser_is_spouse": None,
                "client_is_married": None,
                "children_count": None,
                "client_marriages_count": None,
                "warnings": [f"GENAI_ERROR:{type(e).__name__}"],
            }

    def _build_prompt(self, text: str) -> str:
        # Prompt enfocado a tus reglas (NO→0, null si no hay evidencia, incluye hijastros)
        return f"""
Eres un analista de un PDF "CHECKLIST" de un caso legal. Debes extraer SOLO del texto proporcionado.

Reglas estrictas:
- Si NO puedes determinar algo con evidencia clara, devuelve null.
- children_count cuenta hijos biológicos, hijastros y adoptados si aparecen en el checklist.
- Si el checklist indica "NO", "NINGUNO", "NINGUNA", "NONE" para un conteo, devuelve 0.
- Devuelve SOLO JSON válido que cumpla el schema, sin texto adicional.

Campos:
- abuser_is_spouse: true si el abusador es el cónyuge (esposo/esposa). false si es hijo/hija. null si no se puede inferir.
- client_is_married: true si el cliente está casado actualmente. false si NO está casado actualmente. null si no se puede inferir.
- children_count: número de hijos del cliente (>=0) o null.
- client_marriages_count: número de matrimonios del cliente (>=0) o null.
- warnings: lista de strings (vacía si todo bien).

TEXTO CHECKLIST:
{text}
""".strip()
