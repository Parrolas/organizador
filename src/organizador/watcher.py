"""Non-blocking Downloads observer with a polling safety net."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
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

from organizador.config import AppConfig
from organizador.stabilizer import wait_until_stable

LOGGER = logging.getLogger(__name__)
CandidateCallback = Callable[[Path], None]


class DownloadEventHandler(FileSystemEventHandler):
    """Convert watchdog events into final-path candidates."""

    def __init__(self, enqueue: CandidateCallback) -> None:
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

    def __init__(self, config: AppConfig, on_ready: CandidateCallback) -> None:
        self.config = config
        self.on_ready = on_ready
        self._queue: Queue[Path | None] = Queue()
        self._pending: set[Path] = set()
        self._known: set[Path] = set()
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
    def paused(self) -> bool:
        """Return whether new candidates are temporarily ignored."""

        return self._paused.is_set()

    def start(self) -> None:
        """Start native observation and the safety-net sweeper."""

        if self.running:
            return
        self.config.downloads_dir.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._known = self._snapshot()
        self._observer = Observer()
        self._observer.schedule(
            DownloadEventHandler(self.enqueue), str(self.config.downloads_dir), recursive=False
        )
        self._observer.start()
        self._worker = Thread(target=self._work, name="download-stabilizer", daemon=True)
        self._worker.start()
        self._sweeper = Thread(target=self._sweep, name="download-sweeper", daemon=True)
        self._sweeper.start()
        LOGGER.info("Watching Downloads at %s", self.config.downloads_dir)

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

        if self._stop.is_set() or self._paused.is_set() or not self.config.accepts(path):
            return
        try:
            candidate = path.resolve()
            if candidate.parent != self.config.downloads_dir.resolve():
                return
        except OSError:
            return
        with self._lock:
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

    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                path = self._queue.get(timeout=0.5)
            except Empty:
                continue
            if path is None:
                return
            try:
                stable = wait_until_stable(
                    path,
                    minimum_size=self.config.minimum_file_size,
                    stop_event=self._stop,
                )
                if stable and not self._paused.is_set():
                    self.on_ready(path)
            except Exception:
                LOGGER.exception("Failed while handling download candidate %s", path)
            finally:
                with self._lock:
                    self._pending.discard(path)
                self._queue.task_done()

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
            return {
                path.resolve() for path in self.config.downloads_dir.iterdir() if path.is_file()
            }
        except OSError:
            LOGGER.exception("Could not scan Downloads")
            return set()
