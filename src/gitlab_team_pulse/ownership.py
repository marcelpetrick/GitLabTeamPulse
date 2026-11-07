"""Single owner of the background sync per SQLite database (an advisory lock file).

Only the process holding ``<database>.lock`` runs the scheduler or a CLI refresh; a second
``serve`` on the same database serves the cache and waits for the lock instead of crawling
GitLab a second time. The operating system releases the lock when the owner exits, also after a
crash. Where ``fcntl`` is missing (Windows) the lock is not enforced.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO

try:
    import fcntl
except ImportError:  # pragma: no cover - not available on Windows
    fcntl = None  # type: ignore[assignment]


def lock_path(database_path: Path) -> Path:
    return database_path.with_name(f"{database_path.name}.lock")


class OwnerLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: IO[str] | None = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        """Take the lock without waiting; True when this process owns it now."""
        if self._handle is not None:
            return True
        if fcntl is None:  # pragma: no cover - not available on Windows
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        if fcntl is not None:  # pragma: no branch - fcntl is missing only on Windows
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None
