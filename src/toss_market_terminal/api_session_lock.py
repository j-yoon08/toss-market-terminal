"""Cross-process single-session lock for the Toss Open API OAuth client.

The official contract grants at most one valid access token per OAuth
client: issuing a new token immediately invalidates any token a sibling
process might still be relying on, and there is no refresh token. This
module provides an ``fcntl.flock``-based mutual-exclusion lock so at most
one credential/network-using CLI process (``snapshot``/``account``/
``probe``/``watch``/``live``) runs at a time. Local-only commands
(``watchlist``/``alert``) never touch this lock.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import TracebackType

try:
    import fcntl
except ImportError:  # pragma: no cover - this project targets Linux only.
    fcntl = None  # type: ignore[assignment]

DEFAULT_LOCK_PATH = Path.home() / ".local/state/toss-market-terminal/api-session.lock"
_MAX_MARKER_BYTES = 200
_CONTENTION_MESSAGE = (
    "다른 API/실시간 세션이 이미 실행 중입니다. 먼저 해당 프로세스를 종료한 뒤 다시 시도하세요."
)


class ApiSessionLockError(RuntimeError):
    """Sanitized lock failure: never includes paths, tokens, or credentials."""


def _fail(message: str) -> ApiSessionLockError:
    return ApiSessionLockError(message)


def _verify_existing_directory(path: Path) -> None:
    try:
        info = os.lstat(path)
    except OSError:
        raise _fail("LOCK_DIRECTORY_UNAVAILABLE") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise _fail("UNSAFE_LOCK_DIRECTORY")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
        raise _fail("UNSAFE_LOCK_DIRECTORY")


def _prepare_leaf_directory(parent: Path) -> None:
    """Reject symlink components and create only the final missing directory."""

    if not parent.is_absolute():
        raise _fail("LOCK_PATH_NOT_ABSOLUTE")
    ancestors = list(parent.parents)
    ancestors.reverse()
    for component in [*ancestors, parent]:
        if component == Path(component.anchor):
            continue
        if component == parent and not os.path.lexists(component):
            direct_parent = component.parent
            _verify_existing_directory(direct_parent)
            try:
                os.mkdir(component, 0o700)
            except FileExistsError:
                # Another cooperating writer may have created the same leaf.
                pass
            except OSError:
                raise _fail("LOCK_DIRECTORY_CREATE_FAILED") from None
            _verify_existing_directory(component)
            if stat.S_IMODE(os.lstat(component).st_mode) != 0o700:
                raise _fail("UNSAFE_LOCK_DIRECTORY")
            return
        try:
            info = os.lstat(component)
        except OSError:
            raise _fail("LOCK_PARENT_MISSING") from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise _fail("UNSAFE_LOCK_DIRECTORY")
    _verify_existing_directory(parent)


def _verify_existing_file(path: Path) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError:
        raise _fail("LOCK_FILE_UNAVAILABLE") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise _fail("UNSAFE_LOCK_FILE")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise _fail("UNSAFE_LOCK_FILE")


class ApiSessionLock:
    """Held for the entire lifetime of a credential/network CLI command.

    A persistent lock inode is intentional: the file is never unlinked on
    release, which avoids the create/delete race where a second process
    could recreate the path between another process's unlock and unlink.
    The OS releases the underlying ``flock`` automatically on crash or
    process exit even if :meth:`release` is never reached.
    """

    def __init__(self, path: str | os.PathLike[str] = DEFAULT_LOCK_PATH) -> None:
        self._path = Path(path).expanduser()
        if not self._path.is_absolute():
            raise _fail("LOCK_PATH_NOT_ABSOLUTE")
        self._fd: int | None = None

    def __repr__(self) -> str:
        return "ApiSessionLock(path='<redacted>')"

    def acquire(self) -> None:
        if self._fd is not None:
            return
        _prepare_leaf_directory(self._path.parent)
        _verify_existing_file(self._path)

        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        existed = os.path.lexists(self._path)
        try:
            if existed:
                fd = os.open(self._path, flags)
            else:
                try:
                    fd = os.open(self._path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    # A cooperating process won the create race; validate it.
                    _verify_existing_file(self._path)
                    fd = os.open(self._path, flags)
        except OSError:
            raise _fail("LOCK_FILE_OPEN_FAILED") from None

        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise _fail("UNSAFE_LOCK_FILE")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise _fail(_CONTENTION_MESSAGE) from None
            marker = f"pid={os.getpid()}\n".encode()[:_MAX_MARKER_BYTES]
            os.ftruncate(fd, 0)
            os.write(fd, marker)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> ApiSessionLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


__all__ = ["DEFAULT_LOCK_PATH", "ApiSessionLock", "ApiSessionLockError"]
