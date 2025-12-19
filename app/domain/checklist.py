# app/domain/checklist.py
from dataclasses import dataclass

@dataclass(frozen=True)
class ChecklistSheetPosition:
    row: int
    col_check_abuser_spouse: int
    col_check_client_married: int
    col_check_has_children: int
    col_check_has_marriage: int
    col_status: int
