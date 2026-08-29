from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

APP_CONFIG_DIR = Path.home() / ".config" / "toss-market-terminal"
DEFAULT_CREDENTIALS_PATH = APP_CONFIG_DIR / "credentials.json"
LEGACY_CREDENTIALS_PATH = Path.home() / ".config" / "tossinvest" / "openapi.json"
_MAX_CREDENTIAL_FILE_BYTES = 16_384
_EXPECTED_KEYS = frozenset({"client_id", "client_secret"})


class CredentialError(RuntimeError):
    """Credential data is missing, malformed, or unsafe to access."""


@dataclass(frozen=True, slots=True)
class Credentials:
    client_id: str
    client_secret: str

    @classmethod
    def from_values(cls, client_id: str, client_secret: str) -> Credentials:
        if not isinstance(client_id, str) or not isinstance(client_secret, str):
            raise CredentialError("client_id와 client_secret은 문자열이어야 합니다.")
        if not client_id.startswith("tsck_live_") or len(client_id) <= len("tsck_live_"):
            raise CredentialError("client_id 형식이 올바르지 않습니다.")
        if not client_secret.startswith("tssk_live_") or len(client_secret) <= len("tssk_live_"):
            raise CredentialError("client_secret 형식이 올바르지 않습니다.")
        if any(char.isspace() for char in client_id + client_secret):
            raise CredentialError("자격증명에 공백이나 줄바꿈이 포함되어 있습니다.")
        return cls(client_id=client_id, client_secret=client_secret)

    @classmethod
    def load(cls, path: Path = DEFAULT_CREDENTIALS_PATH) -> Credentials:
        return CredentialStore(path).load()


def _absolute_path(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_absolute():
        return expanded
    return Path.cwd() / expanded


def _path_chain(path: Path) -> tuple[Path, ...]:
    chain: list[Path] = []
    current = path
    while True:
        chain.append(current)
        if current == current.parent:
            break
        current = current.parent
    return tuple(reversed(chain))


def _reject_symlink_ancestors(parent: Path) -> None:
    for component in _path_chain(parent):
        try:
            item_stat = component.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise CredentialError("자격증명 경로 상태를 안전하게 확인하지 못했습니다.") from exc
        if stat.S_ISLNK(item_stat.st_mode):
            raise CredentialError("자격증명 경로의 상위 디렉터리는 심볼릭 링크일 수 없습니다.")
        if not stat.S_ISDIR(item_stat.st_mode):
            raise CredentialError("자격증명 경로의 상위 항목은 디렉터리여야 합니다.")


def _ensure_parent(parent: Path) -> None:
    _reject_symlink_ancestors(parent)
    missing: list[Path] = []
    current = parent
    while True:
        try:
            item_stat = current.lstat()
        except FileNotFoundError:
            missing.append(current)
            if current == current.parent:
                raise CredentialError("자격증명 상위 디렉터리를 만들 수 없습니다.") from None
            current = current.parent
            continue
        except OSError as exc:
            raise CredentialError("자격증명 경로 상태를 안전하게 확인하지 못했습니다.") from exc
        if stat.S_ISLNK(item_stat.st_mode):
            raise CredentialError("자격증명 경로의 상위 디렉터리는 심볼릭 링크일 수 없습니다.")
        if not stat.S_ISDIR(item_stat.st_mode):
            raise CredentialError("자격증명 경로의 상위 항목은 디렉터리여야 합니다.")
        break

    try:
        for component in reversed(missing):
            component.mkdir(mode=0o700)
            component.chmod(0o700)
    except FileExistsError:
        _reject_symlink_ancestors(parent)
    except OSError as exc:
        raise CredentialError("자격증명 상위 디렉터리를 안전하게 만들지 못했습니다.") from exc

    _reject_symlink_ancestors(parent)
    try:
        parent_stat = parent.lstat()
    except OSError as exc:
        raise CredentialError("자격증명 상위 디렉터리를 확인하지 못했습니다.") from exc
    if parent_stat.st_uid != os.getuid():
        raise CredentialError("자격증명 상위 디렉터리의 소유자가 현재 사용자와 다릅니다.")


def _inspect_regular_file(path: Path) -> os.stat_result:
    try:
        file_stat = path.lstat()
    except FileNotFoundError as exc:
        raise CredentialError("자격증명 파일을 찾을 수 없습니다.") from exc
    except OSError as exc:
        raise CredentialError("자격증명 파일 상태를 안전하게 확인하지 못했습니다.") from exc
    if stat.S_ISLNK(file_stat.st_mode):
        raise CredentialError("자격증명 파일은 심볼릭 링크일 수 없습니다.")
    if not stat.S_ISREG(file_stat.st_mode):
        raise CredentialError("자격증명 경로가 일반 파일이 아닙니다.")
    if file_stat.st_uid != os.getuid():
        raise CredentialError("자격증명 파일의 소유자가 현재 사용자와 다릅니다.")
    if stat.S_IMODE(file_stat.st_mode) != 0o600:
        raise CredentialError("자격증명 파일 권한은 정확히 0600이어야 합니다.")
    if file_stat.st_nlink != 1:
        raise CredentialError("자격증명 파일은 하드링크일 수 없습니다.")
    return file_stat


def _read_payload(path: Path, expected: os.stat_result) -> Any:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
                raise CredentialError("자격증명 파일이 확인 중 변경되었습니다.")
            raw = os.read(fd, _MAX_CREDENTIAL_FILE_BYTES + 1)
        finally:
            os.close(fd)
    except CredentialError:
        raise
    except OSError as exc:
        raise CredentialError("자격증명 파일을 안전하게 읽지 못했습니다.") from exc
    if len(raw) > _MAX_CREDENTIAL_FILE_BYTES:
        raise CredentialError("자격증명 파일이 허용 크기를 초과했습니다.")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CredentialError("자격증명 파일을 안전하게 해석하지 못했습니다.") from exc


def _credentials_from_payload(raw: Any) -> Credentials:
    if not isinstance(raw, dict):
        raise CredentialError("자격증명 JSON은 객체여야 합니다.")
    unexpected = set(raw) - _EXPECTED_KEYS
    if unexpected:
        raise CredentialError("자격증명 파일에 알 수 없는 키가 있습니다.")
    client_id = raw.get("client_id")
    client_secret = raw.get("client_secret")
    if not isinstance(client_id, str) or not isinstance(client_secret, str):
        raise CredentialError("client_id와 client_secret은 문자열이어야 합니다.")
    return Credentials.from_values(client_id, client_secret)


def _fsync_directory(parent: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(parent, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class CredentialStore:
    """Private, atomic local storage for the long-lived client credential pair."""

    def __init__(self, path: Path = DEFAULT_CREDENTIALS_PATH) -> None:
        self.path = _absolute_path(path)

    def exists(self) -> bool:
        _reject_symlink_ancestors(self.path.parent)
        try:
            self.path.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise CredentialError("자격증명 파일 상태를 안전하게 확인하지 못했습니다.") from exc
        _inspect_regular_file(self.path)
        return True

    def load(self) -> Credentials:
        _reject_symlink_ancestors(self.path.parent)
        inspected = _inspect_regular_file(self.path)
        return _credentials_from_payload(_read_payload(self.path, inspected))

    def save(self, credentials: Credentials, *, replace: bool = False) -> None:
        validated = Credentials.from_values(credentials.client_id, credentials.client_secret)
        path = self.path
        _ensure_parent(path.parent)

        previous: os.stat_result | None
        try:
            previous = path.lstat()
        except FileNotFoundError:
            previous = None
        except OSError as exc:
            raise CredentialError("자격증명 파일 상태를 안전하게 확인하지 못했습니다.") from exc
        if previous is not None:
            previous = _inspect_regular_file(path)
            if not replace:
                raise CredentialError("자격증명이 이미 설정되어 있습니다.")

        temp_name: str | None = None
        try:
            fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
            os.fchmod(fd, 0o600)
            try:
                payload = (
                    json.dumps(
                        {
                            "client_id": validated.client_id,
                            "client_secret": validated.client_secret,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n"
                )
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise

            if previous is None:
                try:
                    path.lstat()
                except FileNotFoundError:
                    pass
                else:
                    raise CredentialError("자격증명 파일이 저장 중 생성되었습니다.")
            else:
                current = _inspect_regular_file(path)
                if (current.st_dev, current.st_ino) != (previous.st_dev, previous.st_ino):
                    raise CredentialError("자격증명 파일이 저장 중 변경되었습니다.")

            os.replace(temp_name, path)
            temp_name = None
            _fsync_directory(path.parent)
            _inspect_regular_file(path)
        except CredentialError:
            raise
        except OSError as exc:
            raise CredentialError("자격증명 파일을 안전하게 저장하지 못했습니다.") from exc
        finally:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass

    def remove(self) -> None:
        _reject_symlink_ancestors(self.path.parent)
        inspected = _inspect_regular_file(self.path)
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            parent_fd = os.open(self.path.parent, flags)
            try:
                current = os.stat(self.path.name, dir_fd=parent_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (inspected.st_dev, inspected.st_ino):
                    raise CredentialError("자격증명 파일이 삭제 전 변경되었습니다.")
                os.unlink(self.path.name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except CredentialError:
            raise
        except OSError as exc:
            raise CredentialError("자격증명 파일을 안전하게 삭제하지 못했습니다.") from exc


def resolve_credentials_path(
    requested: Path | None = None,
    *,
    default_path: Path = DEFAULT_CREDENTIALS_PATH,
    legacy_path: Path = LEGACY_CREDENTIALS_PATH,
) -> Path:
    if requested is not None:
        return requested.expanduser()
    if default_path.expanduser().exists():
        return default_path.expanduser()
    if legacy_path.expanduser().exists():
        return legacy_path.expanduser()
    return default_path.expanduser()


def mask_client_id(client_id: str) -> str:
    suffix = client_id[-4:] if len(client_id) >= 4 else ""
    prefix = "tsck_live_" if client_id.startswith("tsck_live_") else ""
    return f"{prefix}••••{suffix}"
