from pathlib import Path
from typing import Iterable

from pypdf import PdfReader, PdfWriter


def merge_pdfs_in_order(input_paths: Iterable[str], output_path: str) -> None:
    """
    Merge the provided PDFs in the received order into a final PDF.
    The function assumes paths exist; callers should validate upstream.
    """
    writer = PdfWriter()

    for pdf_path in input_paths:
        reader = PdfReader(str(pdf_path))
        # Agregar todas las páginas de este PDF
        for page in reader.pages:
            writer.add_page(page)

    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with output_file.open("wb") as fh:
        writer.write(fh)
