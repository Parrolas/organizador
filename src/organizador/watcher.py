"""Non-blocking Downloads observer with a polling safety net."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Lock, Thread
from time import monotonic

from watchdog.events import (
    DirCreatedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileMovedEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from organizador.config import MANUAL_IMPORT_BATCH_LIMIT, AppConfig
from organizador.models import ExistingDownload
from organizador.stabilizer import wait_until_stable

LOGGER = logging.getLogger(__name__)
DownloadCandidate = Path | ExistingDownload
PathCandidateCallback = Callable[[Path], None]
CandidateCallback = Callable[[DownloadCandidate], None]
ImportCompleteCallback = Callable[[int], None]


class DownloadEventHandler(FileSystemEventHandler):
    """Convert watchdog events into final-path candidates."""

    def __init__(self, enqueue: PathCandidateCallback) -> None:
        super().__init__()
        self.enqueue = enqueue

    def on_created(self, event: DirCreatedEvent | FileCreatedEvent) -> None:
        if not event.is_directory:
            self.enqueue(Path(os.fsdecode(event.src_path)))

    def on_moved(self, event: DirMovedEvent | FileMovedEvent) -> None:
        if not event.is_directory:
            self.enqueue(Path(os.fsdecode(event.dest_path)))


class DownloadWatcher:
    """Observe final download names without blocking watchdog's event thread."""

    def __init__(
        self,
        config: AppConfig,
        on_ready: CandidateCallback,
        on_import_complete: ImportCompleteCallback | None = None,
    ) -> None:
        self.config = config
        self.on_ready = on_ready
        self.on_import_complete = on_import_complete
        self._queue: Queue[DownloadCandidate | None] = Queue()
        self._pending: set[Path] = set()
        self._known: set[Path] = set()
        self._manual_pending: set[Path] = set()
        self._manual_skipped = 0
        self._ignored_until: dict[Path, float] = {}
        self._lock = Lock()
        self._stop = Event()
        self._paused = Event()
        self._observer: BaseObserver | None = None
        self._worker: Thread | None = None
        self._sweeper: Thread | None = None

    @property
    def running(self) -> bool:
        """Return whether the native observer is active."""

        return self._observer is not None and self._observer.is_alive()

    @property
    def active(self) -> bool:
        """Return whether the shared stabilization worker is active."""

        return self._worker is not None and self._worker.is_alive()

    @property
    def manual_import_running(self) -> bool:
        """Return whether a confirmed existing-file batch is still being checked."""

        with self._lock:
            return bool(self._manual_pending)

    @property
    def paused(self) -> bool:
        """Return whether new candidates are temporarily ignored."""

        return self._paused.is_set()

    def start(self, *, observe: bool = True) -> None:
        """Start the worker and optionally native observation and its safety net."""

        if self.active:
            return
        self.config.downloads_dir.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._known = self._snapshot() if observe else set()
        self._worker = Thread(target=self._work, name="download-stabilizer", daemon=True)
        self._worker.start()
        if observe:
            self._observer = Observer()
            self._observer.schedule(
                DownloadEventHandler(self.enqueue), str(self.config.downloads_dir), recursive=False
            )
            self._observer.start()
            self._sweeper = Thread(target=self._sweep, name="download-sweeper", daemon=True)
            self._sweeper.start()
            LOGGER.info("Watching Downloads at %s", self.config.downloads_dir)
        else:
            LOGGER.info("Manual Downloads import ready at %s", self.config.downloads_dir)

    def stop(self) -> None:
        """Stop all watcher threads cleanly."""

        self._stop.set()
        self._queue.put(None)
        if self._observer is not None and self._observer.is_alive():
            self._observer.stop()
            self._observer.join(timeout=3)
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=3)
        if self._sweeper is not None and self._sweeper.is_alive():
            self._sweeper.join(timeout=3)
        self._observer = None
        self._worker = None
        self._sweeper = None
        with self._lock:
            self._pending.clear()
            self._manual_pending.clear()
            self._manual_skipped = 0

    def set_paused(self, paused: bool) -> None:
        """Pause or resume candidate intake."""

        if paused:
            self._paused.set()
        else:
            self._paused.clear()

    def ignore_self_move(self, path: Path, *, seconds: float = 30.0) -> None:
        """Prevent a file returned by this app from being ingested again."""

        try:
            candidate = path.resolve()
        except OSError:
            return
        with self._lock:
            self._known.add(candidate)
            self._ignored_until[candidate] = monotonic() + seconds

    def enqueue(self, path: Path) -> None:
        """Queue one eligible final filename for stabilization."""

        if self._stop.is_set() or not self.config.accepts(path):
            return
        try:
            candidate = path.absolute()
            if candidate.parent.resolve() != self.config.downloads_dir.resolve():
                return
        except OSError:
            return
        with self._lock:
            if self._paused.is_set():
                self._known.add(candidate)
                return
            ignored_until = self._ignored_until.get(candidate)
            if ignored_until is not None and ignored_until >= monotonic():
                self._known.add(candidate)
                return
            self._ignored_until.pop(candidate, None)
            self._known.add(candidate)
            if candidate in self._pending:
                return
            self._pending.add(candidate)
        self._queue.put(candidate)

    def enqueue_existing(self, candidates: Sequence[ExistingDownload]) -> int:
        """Queue one explicitly confirmed batch while enforcing the fixed safety cap."""

        if self._stop.is_set() or not self.active:
            return 0
        selected = candidates[:MANUAL_IMPORT_BATCH_LIMIT]
        queued: list[ExistingDownload] = []
        skipped = 0
        with self._lock:
            if self._manual_pending:
                return 0
            for candidate in selected:
                path = candidate.path
                try:
                    eligible = (
                        self.config.accepts(path)
                        and path.parent.resolve() == self.config.downloads_dir.resolve()
                        and candidate.still_matches()
                    )
                except OSError:
                    eligible = False
                if not eligible or path in self._pending:
                    skipped += 1
                    continue
                self._pending.add(path)
                self._manual_pending.add(path)
                queued.append(candidate)
            self._manual_skipped = skipped
            if not queued:
                self._manual_skipped = 0
        for candidate in queued:
            self._queue.put(candidate)
        return len(queued)

    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                queued = self._queue.get(timeout=0.5)
            except Empty:
                continue
            if queued is None:
                return
            if isinstance(queued, ExistingDownload):
                manual_candidate = queued
                path = queued.path
            else:
                manual_candidate = None
                path = queued
            delivered = False
            try:
                unchanged_before = manual_candidate is None or manual_candidate.still_matches()
                stable = unchanged_before and wait_until_stable(
                    path, minimum_size=self.config.minimum_file_size, stop_event=self._stop
                )
                unchanged_after = manual_candidate is None or manual_candidate.still_matches()
                if (
                    stable
                    and unchanged_after
                    and (manual_candidate is not None or not self._paused.is_set())
                ):
                    self.on_ready(queued)
                    delivered = True
            except Exception:
                LOGGER.exception("Failed while handling download candidate %s", path)
            finally:
                completed_skips: int | None = None
                with self._lock:
                    self._pending.discard(path)
                    if manual_candidate is not None:
                        if not delivered:
                            self._manual_skipped += 1
                        self._manual_pending.discard(path)
                        if not self._manual_pending:
                            completed_skips = self._manual_skipped
                            self._manual_skipped = 0
                self._queue.task_done()
                if completed_skips is not None and self.on_import_complete is not None:
                    self.on_import_complete(completed_skips)

    def _sweep(self) -> None:
        while not self._stop.wait(20.0):
            current = self._snapshot()
            with self._lock:
                now = monotonic()
                self._ignored_until = {
                    path: deadline
                    for path, deadline in self._ignored_until.items()
                    if deadline >= now
                }
                new_paths = current - self._known
                self._known = current | self._pending
            for path in new_paths:
                self.enqueue(path)

    def _snapshot(self) -> set[Path]:
        try:
            candidates = (
                ExistingDownload.capture(path) for path in self.config.downloads_dir.iterdir()
            )
            return {candidate.path for candidate in candidates if candidate is not None}
        except OSError:
            LOGGER.exception("Could not scan Downloads")
            return set()
