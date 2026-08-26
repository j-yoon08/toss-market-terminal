"""Private append-only audit log for manual live-order outcomes.

Only sanitized :class:`LiveAuditRecord` fields are persisted.  Broker payloads,
account identifiers, approval phrases, tokens, and order IDs have no place in
this schema.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from toss_market_terminal.live_order import LiveAuditRecord
from toss_market_terminal.order_preview import canonical_decimal_text

AUDIT_SCHEMA_VERSION = "1"
DEFAULT_AUDIT_PATH = Path.home() / ".local/state/toss-market-terminal/live-order-audit.jsonl"
MAX_AUDIT_LINE_BYTES = 4096

_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
_CLIENT_ORDER_ID = re.compile(r"tmt-[0-9a-f]{32}")
_SAFE_CODE = re.compile(r"[A-Z0-9_-]{1,80}")
_SYMBOL = re.compile(r"(?:[0-9]{6}|[A-Z][A-Z0-9.-]{0,14})")
_ALLOWED_STATUS = frozenset({"accepted", "rejected", "ambiguous", "blocked"})
_SECRET_MARKERS = ("tsck_live_", "tssk_live_", "bearer ", "confirm live", "client_secret")


class LiveAuditLogError(RuntimeError):
    """Sanitized audit-log failure that never includes paths or record values."""


def _fail(code: str) -> LiveAuditLogError:
    return LiveAuditLogError(code)


def _validate_text(value: object, pattern: re.Pattern[str], code: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise _fail(code)
    lowered = value.lower()
    if any(marker in lowered for marker in _SECRET_MARKERS):
        raise _fail(code)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise _fail(code)
    return value


def _serialize(record: LiveAuditRecord) -> bytes:
    if not isinstance(record, LiveAuditRecord):
        raise _fail("INVALID_AUDIT_RECORD")

    fingerprint = _validate_text(record.fingerprint, _FINGERPRINT, "INVALID_FINGERPRINT")
    client_order_id = _validate_text(
        record.client_order_id, _CLIENT_ORDER_ID, "INVALID_CLIENT_ORDER_ID"
    )
    side = _validate_text(record.side, re.compile(r"BUY|SELL"), "INVALID_SIDE")
    symbol = _validate_text(record.symbol, _SYMBOL, "INVALID_SYMBOL")
    status_value = record.status
    if not isinstance(status_value, str) or status_value not in _ALLOWED_STATUS:
        raise _fail("INVALID_AUDIT_STATUS")
    safe_code = _validate_text(record.safe_code, _SAFE_CODE, "INVALID_SAFE_CODE")

    quantity = record.quantity
    if not isinstance(quantity, Decimal):
        raise _fail("INVALID_QUANTITY")
    if quantity.is_nan() or not quantity.is_finite() or quantity <= 0:
        raise _fail("INVALID_QUANTITY")
    quantity_text = canonical_decimal_text(quantity)
    if len(quantity_text) > 30:
        raise _fail("INVALID_QUANTITY")

    attempted_at = record.attempted_at
    if not isinstance(attempted_at, datetime):
        raise _fail("INVALID_ATTEMPTED_AT")
    if attempted_at.tzinfo is None or attempted_at.utcoffset() is None:
        raise _fail("INVALID_ATTEMPTED_AT")
    attempted_utc = (
        attempted_at.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )

    payload = {
        "schemaVersion": AUDIT_SCHEMA_VERSION,
        "fingerprint": fingerprint,
        "clientOrderId": client_order_id,
        "side": side,
        "symbol": symbol,
        "quantity": quantity_text,
        "attemptedAt": attempted_utc,
        "status": status_value,
        "safeCode": safe_code,
    }
    encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    if len(encoded) > MAX_AUDIT_LINE_BYTES:
        raise _fail("AUDIT_LINE_TOO_LARGE")
    return encoded


def _verify_existing_directory(path: Path) -> None:
    try:
        info = os.lstat(path)
    except OSError:
        raise _fail("AUDIT_DIRECTORY_UNAVAILABLE") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise _fail("UNSAFE_AUDIT_DIRECTORY")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
        raise _fail("UNSAFE_AUDIT_DIRECTORY")


def _prepare_leaf_directory(parent: Path) -> None:
    """Reject symlink components and create only the final missing directory."""

    if not parent.is_absolute():
        raise _fail("AUDIT_PATH_NOT_ABSOLUTE")
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
                # Revalidate it instead of treating the race as success blindly.
                pass
            except OSError:
                raise _fail("AUDIT_DIRECTORY_CREATE_FAILED") from None
            _verify_existing_directory(component)
            if stat.S_IMODE(os.lstat(component).st_mode) != 0o700:
                raise _fail("UNSAFE_AUDIT_DIRECTORY")
            return
        try:
            info = os.lstat(component)
        except OSError:
            raise _fail("AUDIT_PARENT_MISSING") from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise _fail("UNSAFE_AUDIT_DIRECTORY")
    _verify_existing_directory(parent)


def _verify_existing_file(path: Path) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError:
        raise _fail("AUDIT_FILE_UNAVAILABLE") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise _fail("UNSAFE_AUDIT_FILE")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise _fail("UNSAFE_AUDIT_FILE")


class LiveAuditLog:
    """Append sanitized live-order outcomes to a private JSONL file."""

    def __init__(self, path: str | os.PathLike[str] = DEFAULT_AUDIT_PATH) -> None:
        self._path = Path(path).expanduser()
        if not self._path.is_absolute():
            raise _fail("AUDIT_PATH_NOT_ABSOLUTE")

    def __repr__(self) -> str:
        return "LiveAuditLog(path='<redacted>')"

    def _open_verified(self) -> int:
        _prepare_leaf_directory(self._path.parent)
        _verify_existing_file(self._path)

        base_flags = os.O_WRONLY | os.O_APPEND
        base_flags |= getattr(os, "O_CLOEXEC", 0)
        base_flags |= getattr(os, "O_NOFOLLOW", 0)
        existed = os.path.lexists(self._path)
        fd: int | None = None
        try:
            if existed:
                fd = os.open(self._path, base_flags)
            else:
                try:
                    fd = os.open(self._path, base_flags | os.O_CREAT | os.O_EXCL, 0o600)
                    os.fchmod(fd, 0o600)
                except FileExistsError:
                    # A cooperating process won the create race. Validate the
                    # winner before opening it as an existing append target.
                    _verify_existing_file(self._path)
                    fd = os.open(self._path, base_flags)
        except OSError:
            if fd is not None:
                os.close(fd)
            raise _fail("AUDIT_FILE_OPEN_FAILED") from None
        if fd is None:  # defensive: every successful branch above assigns it
            raise _fail("AUDIT_FILE_OPEN_FAILED")
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise _fail("UNSAFE_AUDIT_FILE")
            return fd
        except LiveAuditLogError:
            os.close(fd)
            raise
        except OSError:
            os.close(fd)
            raise _fail("AUDIT_FILE_OPEN_FAILED") from None

    def prepare(self) -> None:
        """Create/verify the private append target before any broker mutation."""

        fd = self._open_verified()
        try:
            os.fsync(fd)
        except OSError:
            raise _fail("AUDIT_FILE_PREPARE_FAILED") from None
        finally:
            os.close(fd)

    def append(self, record: LiveAuditRecord) -> None:
        encoded = _serialize(record)  # validate every field before touching disk
        fd = self._open_verified()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                view = memoryview(encoded)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise _fail("AUDIT_WRITE_FAILED")
                    view = view[written:]
                os.fsync(fd)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except LiveAuditLogError:
            raise
        except OSError:
            raise _fail("AUDIT_WRITE_FAILED") from None
        finally:
            os.close(fd)


__all__ = [
    "AUDIT_SCHEMA_VERSION",
    "DEFAULT_AUDIT_PATH",
    "LiveAuditLog",
    "LiveAuditLogError",
]
