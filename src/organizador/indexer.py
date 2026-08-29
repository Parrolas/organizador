"""Background local text extraction for supported study documents."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from functools import partial
from threading import Event

from pypdf import PdfReader

from organizador.db import Database
from organizador.extractors import OFFICE_SUFFIXES, extract_docx, extract_pptx, extract_xlsx
from organizador.models import FiledDocument

LOGGER = logging.getLogger(__name__)
IndexCallback = Callable[[int, str], None]
MAX_INDEX_BYTES = 50 * 1024 * 1024
INDEXABLE_SUFFIXES = OFFICE_SUFFIXES | {".pdf", ".txt", ".md", ".csv", ".ipynb"}


class DocumentIndexer:
    """Extract document text serially to keep foreground interactions responsive."""

    def __init__(self, database: Database, on_finished: IndexCallback | None = None) -> None:
        self.database = database
        self.on_finished = on_finished
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pdf-indexer")
        self._active: set[int] = set()
        self._stop = Event()

    def submit(self, document: FiledDocument) -> None:
        """Queue a document unless it is already being processed."""

        if self._stop.is_set() or document.id in self._active:
            return
        self._active.add(document.id)
        future = self._executor.submit(self.index_document, document)
        future.add_done_callback(partial(self._done, document.id))

    def submit_pending(self) -> None:
        """Queue documents left unindexed by an earlier app session."""

        for document in self.database.list_unindexed_documents():
            if self._stop.is_set():
                return
            self.submit(document)

    def index_document(self, document: FiledDocument) -> None:
        """Extract and persist text for one supported document."""

        if self._stop.is_set():
            return
        path = document.current_path
        if not path.is_file():
            self.database.mark_document_indexed(document.id, expected_path=document.current_path)
            return
        subject = self.database.get_subject(document.subject_id)
        subject_name = subject.name if subject else ""
        suffix = path.suffix.casefold()
        if suffix in INDEXABLE_SUFFIXES and document.size > MAX_INDEX_BYTES:
            LOGGER.warning("Skipping oversized document index: %s", path)
            self.database.mark_document_indexed(document.id, expected_path=document.current_path)
            return
        pages: list[str] = []
        if suffix == ".pdf":
            reader = PdfReader(path)
            if reader.is_encrypted:
                try:
                    reader.decrypt("")
                except Exception:
                    self.database.mark_document_indexed(
                        document.id, expected_path=document.current_path
                    )
                    return
            for page in reader.pages:
                if self._stop.is_set():
                    return
                pages.append((page.extract_text() or "").strip())
        elif suffix in {".txt", ".md", ".csv"}:
            if self._stop.is_set():
                return
            pages = [path.read_text(encoding="utf-8", errors="replace")]
        elif suffix == ".ipynb":
            if self._stop.is_set():
                return
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            cells = payload.get("cells", [])
            pages = [
                "".join(str(part) for part in cell.get("source", []))
                for cell in cells
                if isinstance(cell, dict)
            ]
        elif suffix in OFFICE_SUFFIXES:
            try:
                if suffix == ".docx":
                    pages = extract_docx(path)
                elif suffix == ".pptx":
                    pages = extract_pptx(path)
                else:
                    pages = extract_xlsx(path)
            except Exception:
                LOGGER.exception("Failed to extract Office document %s", path)
                self.database.mark_document_indexed(
                    document.id, expected_path=document.current_path
                )
                return
        if not self._stop.is_set():
            self.database.replace_document_pages(
                document.id,
                subject_name,
                document.original_name,
                pages,
                expected_path=document.current_path,
            )

    def shutdown(self) -> None:
        """Stop accepting work and cancel jobs that have not started."""

        self._stop.set()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _done(self, file_id: int, future: Future[None]) -> None:
        self._active.discard(file_id)
        error = ""
        try:
            future.result()
        except CancelledError:
            return
        except Exception as exc:
            LOGGER.exception("Failed to index file id %s", file_id)
            error = str(exc)
        if self.on_finished is not None:
            self.on_finished(file_id, error)
