"""Text extraction for modern Office Open XML study documents."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from docx import Document
from openpyxl import load_workbook
from pptx import Presentation

OFFICE_SUFFIXES = frozenset({".docx", ".pptx", ".xlsx"})
# Worst-case text kept per document while extracting. The indexer trims to
# its own limit afterwards, but extraction must stop early: a few kilobytes
# of compressed Office XML can expand to millions of characters.
MAX_EXTRACTION_CHARS = 2_000_000


class ExtractionBudget:
    """Bound accumulated extraction text before it can bloat memory."""

    def __init__(self, max_chars: int = MAX_EXTRACTION_CHARS) -> None:
        if max_chars < 1:
            raise ValueError("extraction budget must be positive")
        self.max_chars = max_chars
        self.total = 0
        self.exhausted = False

    def take(self, text: str) -> str:
        """Accept up to the remaining budget and report exhaustion."""

        if self.exhausted:
            return ""
        remaining = self.max_chars - self.total
        if remaining <= 0:
            self.exhausted = True
            return ""
        accepted = text[:remaining]
        self.total += len(accepted)
        if len(accepted) < len(text):
            self.exhausted = True
        return accepted


def extract_docx(path: Path, *, max_chars: int = MAX_EXTRACTION_CHARS) -> list[str]:
    """Extract paragraphs and table cells from a Word document."""

    budget = ExtractionBudget(max_chars)
    document = Document(str(path))
    parts: list[str] = []
    for paragraph in document.paragraphs:
        if budget.exhausted:
            break
        text = paragraph.text.strip()
        if text:
            parts.append(budget.take(text))
    for table in document.tables:
        if budget.exhausted:
            break
        for row in table.rows:
            if budget.exhausted:
                break
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                parts.append(budget.take("\t".join(cells)))
    return ["\n".join(parts)]


def extract_pptx(path: Path, *, max_chars: int = MAX_EXTRACTION_CHARS) -> list[str]:
    """Extract text and tables while preserving PowerPoint slide numbers."""

    budget = ExtractionBudget(max_chars)
    presentation = Presentation(str(path))
    pages: list[str] = []
    for slide in presentation.slides:
        if budget.exhausted:
            break
        parts: list[str] = []
        for shape in slide.shapes:
            if budget.exhausted:
                break
            if shape.has_text_frame:
                text = shape.text.strip()
                if text:
                    parts.append(budget.take(text))
            if shape.has_table:
                for row in shape.table.rows:
                    if budget.exhausted:
                        break
                    cells = [cell.text.strip() for cell in row.cells]
                    if any(cells):
                        parts.append(budget.take("\t".join(cells)))
        pages.append("\n".join(parts))
    return pages


def extract_xlsx(path: Path, *, max_chars: int = MAX_EXTRACTION_CHARS) -> list[str]:
    """Extract cell values while preserving Excel worksheet numbers."""

    budget = ExtractionBudget(max_chars)
    workbook = load_workbook(str(path), read_only=True, data_only=False, keep_links=False)
    try:
        pages: list[str] = []
        for worksheet in workbook.worksheets:
            if budget.exhausted:
                break
            rows = [budget.take(worksheet.title)]
            for row in worksheet.iter_rows(values_only=True):
                if budget.exhausted:
                    break
                values = [budget.take(_cell_text(value)) for value in row]
                if any(values):
                    rows.append("\t".join(values).rstrip())
            pages.append("\n".join(rows))
        return pages
    finally:
        workbook.close()


def _cell_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)
