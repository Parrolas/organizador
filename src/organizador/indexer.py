"""Background local text extraction for supported study documents."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from functools import partial
from pathlib import Path
from threading import Event

from pypdf import PdfReader

from organizador.db import Database
from organizador.extractors import OFFICE_SUFFIXES, extract_docx, extract_pptx, extract_xlsx
from organizador.models import ExistingDownload, FiledDocument

LOGGER = logging.getLogger(__name__)
IndexCallback = Callable[[int, str], None]
MAX_INDEX_BYTES = 50 * 1024 * 1024
MAX_INDEX_CHARS = 1_000_000
INDEXABLE_SUFFIXES = OFFICE_SUFFIXES | {".pdf", ".txt", ".md", ".csv", ".ipynb"}
MAX_PENDING_INDEX_JOBS = 50
DocumentKey = tuple[int, str]


def _cap_pages(pages: list[str], limit: int = MAX_INDEX_CHARS) -> tuple[list[str], bool]:
    """Truncate extracted text so one document cannot bloat the search index."""

    if limit < 1:
        raise ValueError("index character limit must be positive")
    kept: list[str] = []
    total = 0
    for page in pages:
        if total >= limit:
            break
        kept.append(page[: limit - total])
        total += len(kept[-1])
    return kept, total < sum(len(page) for page in pages)


class DocumentIndexer:
    """Extract document text serially to keep foreground interactions responsive."""

    def __init__(self, database: Database, on_finished: IndexCallback | None = None) -> None:
        self.database = database
        self.on_finished = on_finished
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pdf-indexer")
        self._active: set[DocumentKey] = set()
        self._attempted: set[DocumentKey] = set()
        self._stop = Event()

    def submit(self, document: FiledDocument) -> None:
        """Queue a document unless it is already being processed."""

        key = (document.id, document.record_token)
        if (
            self._stop.is_set()
            or key in self._attempted
            or len(self._active) >= MAX_PENDING_INDEX_JOBS
        ):
            return
        self._active.add(key)
        self._attempted.add(key)
        future = self._executor.submit(self.index_document, document)
        future.add_done_callback(partial(self._done, key))

    def submit_pending(self) -> None:
        """Queue documents left unindexed by an earlier app session."""

        available = MAX_PENDING_INDEX_JOBS - len(self._active)
        if available <= 0:
            return
        for document in self.database.list_unindexed_documents(
            limit=available,
            excluded_keys=self._attempted,
        ):
            if self._stop.is_set():
                return
            self.submit(document)

    def reindex(self, document: FiledDocument) -> None:
        """Queue a document again even if a previous attempt finished or failed."""

        key = (document.id, document.record_token)
        self._attempted.discard(key)
        self.database.clear_index_state(
            document.id,
            expected_path=document.current_path,
            expected_record_token=document.record_token,
        )
        self.submit(document)

    def index_document(self, document: FiledDocument) -> None:
        """Extract and persist text for one supported document."""

        if self._stop.is_set():
            return
        path = document.current_path
        if ExistingDownload.capture(path) is None:
            LOGGER.warning("Deferring index because the document is missing or unsafe: %s", path)
            return
        try:
            current_size = path.stat().st_size
        except OSError:
            LOGGER.warning("Deferring index because the document cannot be statted: %s", path)
            return
        if current_size != document.size:
            LOGGER.info("Requeuing index after on-disk change: %s", path)
            self.database.refresh_file_size(
                document.id,
                current_size,
                expected_path=document.current_path,
                expected_record_token=document.record_token,
            )
            # Let the refill pass queue the refreshed record; otherwise the new
            # size would never be extracted.
            self._attempted.discard((document.id, document.record_token))
            return
        final_name = path.name
        subject = self.database.get_subject(document.subject_id)
        subject_name = subject.name if subject else ""
        suffix = path.suffix.casefold()
        if suffix in INDEXABLE_SUFFIXES and current_size > MAX_INDEX_BYTES:
            LOGGER.warning("Skipping oversized document index: %s", path)
            self._store_name_only(document, subject_name, final_name)
            self._mark_failed(document, state="too_large", error="")
            return
        try:
            pages = self._extract_text(document, path, suffix)
            extract_error: str | None = None
        except Exception as exc:
            LOGGER.exception("Failed to extract document %s", path)
            pages = []
            extract_error = str(exc) or type(exc).__name__
        if self._stop.is_set():
            return
        capped, truncated = _cap_pages(pages)
        if truncated:
            LOGGER.warning("Truncated indexed text for %s at %d characters", path, MAX_INDEX_CHARS)
        if not any(page.strip() for page in capped):
            # Documents without extractable text stay findable by their final name.
            capped = [final_name]
        self.database.replace_document_pages(
            document.id,
            subject_name,
            final_name,
            capped,
            expected_path=document.current_path,
            expected_record_token=document.record_token,
        )
        if extract_error is not None:
            self._mark_failed(document, state="failed", error=extract_error)

    def _store_name_only(self, document: FiledDocument, subject_name: str, final_name: str) -> None:
        self.database.replace_document_pages(
            document.id,
            subject_name,
            final_name,
            [final_name],
            expected_path=document.current_path,
            expected_record_token=document.record_token,
        )

    def _mark_failed(self, document: FiledDocument, *, state: str, error: str) -> None:
        self.database.mark_document_indexed(
            document.id,
            expected_path=document.current_path,
            expected_record_token=document.record_token,
            index_state=state,
            index_error=error,
        )

    def _extract_text(self, document: FiledDocument, path: Path, suffix: str) -> list[str]:
        """Extract raw page texts; failures propagate to a failed index state."""

        if self._stop.is_set():
            return []
        if suffix == ".pdf":
            return self._extract_pdf(document, path)
        if suffix in {".txt", ".md", ".csv"}:
            return [path.read_text(encoding="utf-8", errors="replace")]
        if suffix == ".ipynb":
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            cells = payload.get("cells", [])
            return [
                "".join(str(part) for part in cell.get("source", []))
                for cell in cells
                if isinstance(cell, dict)
            ]
        if suffix in OFFICE_SUFFIXES:
            if suffix == ".docx":
                return extract_docx(path)
            if suffix == ".pptx":
                return extract_pptx(path)
            return extract_xlsx(path)
        return []

    def _extract_pdf(self, document: FiledDocument, path: Path) -> list[str]:
        reader = PdfReader(path)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:
                raise OSError("PDF protegido por palavra-passe.") from exc
        pages: list[str] = []
        for page in reader.pages:
            if self._stop.is_set():
                return []
            pages.append((page.extract_text() or "").strip())
        return pages

    def shutdown(self) -> None:
        """Stop accepting work and cancel jobs that have not started."""

        self._stop.set()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _done(self, key: DocumentKey, future: Future[None]) -> None:
        self._active.discard(key)
        error = ""
        try:
            future.result()
        except CancelledError:
            return
        except Exception as exc:
            LOGGER.exception("Failed to index file id %s", key[0])
            error = str(exc)
        if self.on_finished is not None:
            self.on_finished(key[0], error)
