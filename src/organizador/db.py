"""SQLite persistence with page-level FTS5 document search."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from organizador.models import (
    FILE_KINDS,
    FiledDocument,
    FilingHint,
    HistoryEvent,
    InboxItem,
    SearchResult,
    StudyTask,
    Subject,
)

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS subjects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    code TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '#2D7A78',
    keywords_json TEXT NOT NULL DEFAULT '[]',
    folder_name TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbox (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    original_path TEXT NOT NULL,
    original_name TEXT NOT NULL,
    size INTEGER NOT NULL,
    detected_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    suggested_subject_id INTEGER REFERENCES subjects(id) ON DELETE SET NULL,
    suggested_kind TEXT NOT NULL DEFAULT 'Outros',
    last_error TEXT NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_inbox_active_path
ON inbox(path) WHERE status IN ('pending', 'error', 'filing');

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL REFERENCES subjects(id),
    inbox_id INTEGER REFERENCES inbox(id),
    kind TEXT NOT NULL,
    original_name TEXT NOT NULL,
    current_path TEXT NOT NULL UNIQUE,
    original_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    filed_at TEXT NOT NULL,
    indexed_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,
    source_path TEXT NOT NULL,
    destination_path TEXT NOT NULL,
    file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
    inbox_id INTEGER REFERENCES inbox(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    undone_at TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    subject_id INTEGER REFERENCES subjects(id) ON DELETE SET NULL,
    file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
    due_date TEXT,
    completed INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS document_pages USING fts5(
    file_id UNINDEXED,
    page UNINDEXED,
    subject UNINDEXED,
    title,
    content,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE INDEX IF NOT EXISTS idx_inbox_status_detected ON inbox(status, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_files_filed_at ON files(filed_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_due_date ON tasks(completed, due_date);
CREATE INDEX IF NOT EXISTS idx_events_undo ON events(undone_at, created_at DESC);
"""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Database:
    """Small connection-per-operation SQLite repository.

    Background workers may call this class safely because connections are never
    shared across threads.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Open a configured database connection."""

        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create the database and all current schema objects."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            version_row = connection.execute("PRAGMA user_version").fetchone()
            previous_version = int(version_row[0]) if version_row is not None else 0
            connection.executescript(SCHEMA)
            if previous_version < 2:
                self._prepare_office_reindex(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()

    @staticmethod
    def _prepare_office_reindex(connection: sqlite3.Connection) -> None:
        """Queue Office files handled before text extraction was supported."""

        office_files = """
            SELECT id FROM files
            WHERE LOWER(current_path) LIKE '%.docx'
               OR LOWER(current_path) LIKE '%.pptx'
               OR LOWER(current_path) LIKE '%.xlsx'
        """
        connection.execute(
            f"""DELETE FROM document_pages
                 WHERE CAST(file_id AS INTEGER) IN ({office_files})"""
        )
        connection.execute(
            f"""UPDATE files SET indexed_at = NULL
                 WHERE id IN ({office_files})"""
        )

    def add_subject(
        self,
        name: str,
        code: str,
        color: str,
        keywords: Sequence[str],
        folder_name: str,
    ) -> Subject:
        """Create and return a subject."""

        cleaned_keywords = tuple(dict.fromkeys(item.strip() for item in keywords if item.strip()))
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO subjects(name, code, color, keywords_json, folder_name, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    name.strip(),
                    code.strip(),
                    color,
                    json.dumps(cleaned_keywords, ensure_ascii=False),
                    folder_name,
                    _now(),
                ),
            )
            connection.commit()
            if cursor.lastrowid is None:
                raise RuntimeError("A base de dados não devolveu o id da disciplina.")
            subject_id = cursor.lastrowid
        subject = self.get_subject(subject_id)
        if subject is None:  # pragma: no cover - defensive database invariant
            raise RuntimeError("A disciplina foi criada mas não pôde ser lida.")
        return subject

    def update_subject(
        self,
        subject_id: int,
        name: str,
        code: str,
        color: str,
        keywords: Sequence[str],
        folder_name: str,
    ) -> Subject:
        """Update an existing subject."""

        cleaned_keywords = tuple(dict.fromkeys(item.strip() for item in keywords if item.strip()))
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE subjects
                SET name = ?, code = ?, color = ?, keywords_json = ?, folder_name = ?
                WHERE id = ?
                """,
                (
                    name.strip(),
                    code.strip(),
                    color,
                    json.dumps(cleaned_keywords, ensure_ascii=False),
                    folder_name,
                    subject_id,
                ),
            )
            connection.commit()
        subject = self.get_subject(subject_id)
        if subject is None:
            raise LookupError(f"Disciplina inexistente: {subject_id}")
        return subject

    def archive_subject(self, subject_id: int) -> None:
        """Hide a subject without deleting documents linked to it."""

        with self.connect() as connection:
            connection.execute("UPDATE subjects SET active = 0 WHERE id = ?", (subject_id,))
            connection.commit()

    def delete_subject(self, subject_id: int) -> None:
        """Delete an unused subject while rolling back failed first-run setup."""

        with self.connect() as connection:
            connection.execute("DELETE FROM subjects WHERE id = ?", (subject_id,))
            connection.commit()

    def get_subject(self, subject_id: int) -> Subject | None:
        """Return a subject by id."""

        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM subjects WHERE id = ?", (subject_id,)
            ).fetchone()
        return self._subject(row) if row else None

    def list_subjects(self, *, active_only: bool = True) -> list[Subject]:
        """List subjects alphabetically."""

        query = "SELECT * FROM subjects"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY name COLLATE NOCASE"
        with self.connect() as connection:
            rows = connection.execute(query).fetchall()
        return [self._subject(row) for row in rows]

    def count_subjects(self) -> int:
        """Return the number of active subjects."""

        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM subjects WHERE active = 1"
            ).fetchone()
        return int(row["total"])

    def add_inbox_item(
        self,
        path: Path,
        original_path: Path,
        original_name: str,
        size: int,
    ) -> InboxItem:
        """Record a file moved into the university inbox."""

        now = _now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO inbox(path, original_path, original_name, size, detected_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (str(path), str(original_path), original_name, size, now),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("A base de dados não devolveu o id do ficheiro recebido.")
            inbox_id = cursor.lastrowid
            connection.execute(
                """
                INSERT INTO events(action, source_path, destination_path, inbox_id, created_at)
                VALUES ('ingest', ?, ?, ?, ?)
                """,
                (str(original_path), str(path), inbox_id, now),
            )
            connection.commit()
        item = self.get_inbox_item(inbox_id)
        if item is None:  # pragma: no cover - defensive database invariant
            raise RuntimeError("O ficheiro entrou na caixa mas não pôde ser lido.")
        return item

    def get_inbox_item(self, inbox_id: int) -> InboxItem | None:
        """Return an inbox item by id."""

        with self.connect() as connection:
            row = connection.execute("SELECT * FROM inbox WHERE id = ?", (inbox_id,)).fetchone()
        return self._inbox(row) if row else None

    def find_active_inbox_by_path(self, path: Path) -> InboxItem | None:
        """Find an existing active inbox record for a path."""

        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM inbox
                WHERE path = ? AND status IN ('pending', 'error', 'filing')
                ORDER BY id DESC LIMIT 1
                """,
                (str(path),),
            ).fetchone()
        return self._inbox(row) if row else None

    def list_inbox_items(self) -> list[InboxItem]:
        """List files still requiring attention."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM inbox
                WHERE status IN ('pending', 'error')
                ORDER BY detected_at DESC
                """
            ).fetchall()
        return [self._inbox(row) for row in rows]

    def count_inbox_items(self) -> int:
        """Return the current inbox count."""

        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM inbox WHERE status IN ('pending', 'error')"
            ).fetchone()
        return int(row["total"])

    def update_inbox_suggestion(self, inbox_id: int, subject_id: int | None, kind: str) -> None:
        """Store the classifier proposal shown to the user."""

        with self.connect() as connection:
            connection.execute(
                """
                UPDATE inbox
                SET suggested_subject_id = ?, suggested_kind = ?, last_error = ''
                WHERE id = ?
                """,
                (subject_id, kind, inbox_id),
            )
            connection.commit()

    def set_inbox_status(self, inbox_id: int, status: str, error: str = "") -> None:
        """Change processing state for an inbox item."""

        with self.connect() as connection:
            connection.execute(
                "UPDATE inbox SET status = ?, last_error = ? WHERE id = ?",
                (status, error, inbox_id),
            )
            connection.commit()

    def record_filing(
        self,
        inbox_id: int,
        subject_id: int,
        kind: str,
        destination_path: Path,
    ) -> FiledDocument:
        """Atomically record a successful inbox-to-subject move."""

        item = self.get_inbox_item(inbox_id)
        if item is None:
            raise LookupError(f"Ficheiro da caixa inexistente: {inbox_id}")
        now = _now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO files(
                    subject_id, inbox_id, kind, original_name, current_path,
                    original_path, size, filed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    subject_id,
                    inbox_id,
                    kind,
                    item.original_name,
                    str(destination_path),
                    str(item.original_path),
                    item.size,
                    now,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("A base de dados não devolveu o id do documento.")
            file_id = cursor.lastrowid
            connection.execute(
                "UPDATE inbox SET status = 'filed', path = ?, last_error = '' WHERE id = ?",
                (str(destination_path), inbox_id),
            )
            connection.execute(
                """
                INSERT INTO events(
                    action, source_path, destination_path, file_id, inbox_id, created_at
                ) VALUES ('file', ?, ?, ?, ?, ?)
                """,
                (str(item.path), str(destination_path), file_id, inbox_id, now),
            )
            connection.commit()
        document = self.get_file(file_id)
        if document is None:  # pragma: no cover - defensive database invariant
            raise RuntimeError("O ficheiro foi organizado mas não pôde ser lido.")
        return document

    def record_return(self, inbox_id: int, destination_path: Path) -> None:
        """Record an inbox file returned to Downloads as non-university material."""

        item = self.get_inbox_item(inbox_id)
        if item is None:
            raise LookupError(f"Ficheiro da caixa inexistente: {inbox_id}")
        now = _now()
        with self.connect() as connection:
            connection.execute(
                "UPDATE inbox SET status = 'returned', path = ?, last_error = '' WHERE id = ?",
                (str(destination_path), inbox_id),
            )
            connection.execute(
                """
                INSERT INTO events(action, source_path, destination_path, inbox_id, created_at)
                VALUES ('return', ?, ?, ?, ?)
                """,
                (str(item.path), str(destination_path), inbox_id, now),
            )
            connection.commit()

    def get_file(self, file_id: int) -> FiledDocument | None:
        """Return a filed document by id."""

        with self.connect() as connection:
            row = connection.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        return self._file(row) if row else None

    def list_recent_files(self, limit: int = 8) -> list[FiledDocument]:
        """List the latest filed documents."""

        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM files ORDER BY filed_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._file(row) for row in rows]

    def filing_hints(self) -> list[FilingHint]:
        """Return live, confirmed filing choices for conservative local learning."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT f.inbox_id, f.original_name, f.subject_id, f.kind, f.current_path
                FROM files AS f
                JOIN inbox AS i ON i.id = f.inbox_id
                JOIN subjects AS s ON s.id = f.subject_id
                WHERE i.status = 'filed'
                  AND s.active = 1
                  AND EXISTS (
                      SELECT 1 FROM events AS e
                      WHERE e.action = 'file'
                        AND e.file_id = f.id
                        AND e.inbox_id = f.inbox_id
                        AND e.undone_at IS NULL
                  )
                ORDER BY f.filed_at ASC, f.id ASC
                """
            ).fetchall()

        hints: list[FilingHint] = []
        seen_inbox_ids: set[int] = set()
        for row in rows:
            inbox_id = int(row["inbox_id"])
            kind = str(row["kind"])
            if inbox_id in seen_inbox_ids or kind not in FILE_KINDS:
                continue
            if not Path(str(row["current_path"])).is_file():
                continue
            seen_inbox_ids.add(inbox_id)
            hints.append(FilingHint(str(row["original_name"]), int(row["subject_id"]), kind))
        return hints

    def list_unindexed_documents(self, limit: int = 50) -> list[FiledDocument]:
        """List supported files waiting for text extraction."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM files
                WHERE indexed_at IS NULL
                ORDER BY filed_at ASC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._file(row) for row in rows]

    def latest_undoable_filing(self) -> HistoryEvent | None:
        """Return the most recent filing that has not been undone."""

        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM events
                WHERE action = 'file' AND undone_at IS NULL
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
        return self._event(row) if row else None

    def mark_filing_undone(self, event: HistoryEvent, restored_path: Path) -> None:
        """Update persistence after the filer restores a document to the inbox."""

        if event.file_id is None or event.inbox_id is None:
            raise ValueError("O evento não contém os dados necessários para desfazer.")
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM document_pages WHERE file_id = ?", (str(event.file_id),)
            )
            connection.execute("DELETE FROM files WHERE id = ?", (event.file_id,))
            connection.execute(
                """
                UPDATE inbox
                SET status = 'pending', path = ?, last_error = ''
                WHERE id = ?
                """,
                (str(restored_path), event.inbox_id),
            )
            connection.execute("UPDATE events SET undone_at = ? WHERE id = ?", (_now(), event.id))
            connection.commit()

    def replace_document_pages(
        self,
        file_id: int,
        subject: str,
        title: str,
        pages: Sequence[str],
        *,
        expected_path: Path | None = None,
    ) -> None:
        """Replace all indexed pages for a document."""

        with self.connect() as connection:
            if expected_path is not None:
                updated = connection.execute(
                    """
                    UPDATE files SET indexed_at = ?
                    WHERE id = ? AND current_path = ?
                    """,
                    (_now(), file_id, str(expected_path)),
                )
                if updated.rowcount != 1:
                    return
            else:
                connection.execute(
                    "UPDATE files SET indexed_at = ? WHERE id = ?", (_now(), file_id)
                )
            connection.execute("DELETE FROM document_pages WHERE file_id = ?", (str(file_id),))
            connection.executemany(
                """
                INSERT INTO document_pages(file_id, page, subject, title, content)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (str(file_id), str(index), subject, title, content)
                    for index, content in enumerate(pages, start=1)
                    if content.strip()
                ],
            )
            connection.commit()

    def mark_document_indexed(self, file_id: int, *, expected_path: Path | None = None) -> None:
        """Mark a document handled even when it contains no extractable text."""

        with self.connect() as connection:
            if expected_path is not None:
                connection.execute(
                    """
                    UPDATE files SET indexed_at = ?
                    WHERE id = ? AND current_path = ?
                    """,
                    (_now(), file_id, str(expected_path)),
                )
            else:
                connection.execute(
                    "UPDATE files SET indexed_at = ? WHERE id = ?", (_now(), file_id)
                )
            connection.commit()

    def search(self, text: str, limit: int = 40) -> list[SearchResult]:
        """Search indexed pages using safe prefix terms."""

        query = self._fts_query(text)
        if not query:
            return []
        try:
            with self.connect() as connection:
                rows = connection.execute(
                    """
                    SELECT
                        f.id AS file_id,
                        f.current_path AS path,
                        f.original_name AS title,
                        s.name AS subject_name,
                        f.kind AS kind,
                        CAST(dp.page AS INTEGER) AS page,
                        snippet(document_pages, 4, '[', ']', ' … ', 20) AS excerpt
                    FROM document_pages AS dp
                    JOIN files AS f ON f.id = CAST(dp.file_id AS INTEGER)
                    JOIN subjects AS s ON s.id = f.subject_id
                    WHERE document_pages MATCH ?
                    ORDER BY bm25(document_pages), f.filed_at DESC
                    LIMIT ?
                    """,
                    (query, limit),
                ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [
            SearchResult(
                file_id=int(row["file_id"]),
                path=Path(str(row["path"])),
                title=str(row["title"]),
                subject_name=str(row["subject_name"]),
                kind=str(row["kind"]),
                page=int(row["page"]),
                snippet=str(row["excerpt"]),
            )
            for row in rows
        ]

    def add_task(
        self,
        title: str,
        subject_id: int | None,
        due_date: date | None,
        file_id: int | None = None,
    ) -> StudyTask:
        """Create a study task."""

        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO tasks(title, subject_id, file_id, due_date, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    title.strip(),
                    subject_id,
                    file_id,
                    due_date.isoformat() if due_date else None,
                    _now(),
                ),
            )
            connection.commit()
            if cursor.lastrowid is None:
                raise RuntimeError("A base de dados não devolveu o id da tarefa.")
            task_id = cursor.lastrowid
        task = self.get_task(task_id)
        if task is None:  # pragma: no cover - defensive database invariant
            raise RuntimeError("A tarefa foi criada mas não pôde ser lida.")
        return task

    def get_task(self, task_id: int) -> StudyTask | None:
        """Return a task by id."""

        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT t.*, COALESCE(s.name, '') AS subject_name
                FROM tasks AS t
                LEFT JOIN subjects AS s ON s.id = t.subject_id
                WHERE t.id = ?
                """,
                (task_id,),
            ).fetchone()
        return self._task(row) if row else None

    def list_tasks(self, *, include_completed: bool = True) -> list[StudyTask]:
        """List tasks with incomplete work first and nearest deadlines next."""

        where = "" if include_completed else "WHERE t.completed = 0"
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT t.*, COALESCE(s.name, '') AS subject_name
                FROM tasks AS t
                LEFT JOIN subjects AS s ON s.id = t.subject_id
                {where}
                ORDER BY t.completed, t.due_date IS NULL, t.due_date, t.created_at DESC
                """
            ).fetchall()
        return [self._task(row) for row in rows]

    def set_task_completed(self, task_id: int, completed: bool) -> None:
        """Mark a task complete or reopen it."""

        with self.connect() as connection:
            connection.execute(
                "UPDATE tasks SET completed = ? WHERE id = ?", (int(completed), task_id)
            )
            connection.commit()

    def delete_task(self, task_id: int) -> None:
        """Delete a task explicitly selected by the user."""

        with self.connect() as connection:
            connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            connection.commit()

    @staticmethod
    def _fts_query(text: str) -> str:
        terms = [part.replace('"', '""') for part in text.split() if part.strip()]
        return " AND ".join(f'"{term}"*' for term in terms)

    @staticmethod
    def _subject(row: sqlite3.Row) -> Subject:
        return Subject(
            id=int(row["id"]),
            name=str(row["name"]),
            code=str(row["code"]),
            color=str(row["color"]),
            keywords=tuple(json.loads(str(row["keywords_json"]))),
            folder_name=str(row["folder_name"]),
            active=bool(row["active"]),
        )

    @staticmethod
    def _inbox(row: sqlite3.Row) -> InboxItem:
        return InboxItem(
            id=int(row["id"]),
            path=Path(str(row["path"])),
            original_path=Path(str(row["original_path"])),
            original_name=str(row["original_name"]),
            size=int(row["size"]),
            detected_at=datetime.fromisoformat(str(row["detected_at"])),
            status=str(row["status"]),
            suggested_subject_id=(
                int(row["suggested_subject_id"])
                if row["suggested_subject_id"] is not None
                else None
            ),
            suggested_kind=str(row["suggested_kind"]),
            last_error=str(row["last_error"]),
        )

    @staticmethod
    def _file(row: sqlite3.Row) -> FiledDocument:
        return FiledDocument(
            id=int(row["id"]),
            subject_id=int(row["subject_id"]),
            kind=str(row["kind"]),
            original_name=str(row["original_name"]),
            current_path=Path(str(row["current_path"])),
            original_path=Path(str(row["original_path"])),
            size=int(row["size"]),
            filed_at=datetime.fromisoformat(str(row["filed_at"])),
            indexed_at=_parse_datetime(
                str(row["indexed_at"]) if row["indexed_at"] is not None else None
            ),
        )

    @staticmethod
    def _event(row: sqlite3.Row) -> HistoryEvent:
        return HistoryEvent(
            id=int(row["id"]),
            action=str(row["action"]),
            source_path=Path(str(row["source_path"])),
            destination_path=Path(str(row["destination_path"])),
            file_id=int(row["file_id"]) if row["file_id"] is not None else None,
            inbox_id=int(row["inbox_id"]) if row["inbox_id"] is not None else None,
            created_at=datetime.fromisoformat(str(row["created_at"])),
            undone_at=_parse_datetime(
                str(row["undone_at"]) if row["undone_at"] is not None else None
            ),
        )

    @staticmethod
    def _task(row: sqlite3.Row) -> StudyTask:
        due = str(row["due_date"]) if row["due_date"] is not None else None
        return StudyTask(
            id=int(row["id"]),
            title=str(row["title"]),
            subject_id=int(row["subject_id"]) if row["subject_id"] is not None else None,
            subject_name=str(row["subject_name"]),
            file_id=int(row["file_id"]) if row["file_id"] is not None else None,
            due_date=date.fromisoformat(due) if due else None,
            completed=bool(row["completed"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )
