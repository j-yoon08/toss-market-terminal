from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CREDENTIALS_PATH = Path.home() / ".config" / "tossinvest" / "openapi.json"


class CredentialError(RuntimeError):
    """Credential file is missing, malformed, or unsafe to read."""


@dataclass(frozen=True, slots=True)
class Credentials:
    client_id: str
    client_secret: str

    @classmethod
    def load(cls, path: Path = DEFAULT_CREDENTIALS_PATH) -> Credentials:
        path = path.expanduser()
        try:
            if path.is_symlink():
                raise CredentialError("자격증명 파일은 심볼릭 링크일 수 없습니다.")
            file_stat = path.stat()
        except CredentialError:
            raise
        except FileNotFoundError as exc:
            raise CredentialError(f"자격증명 파일을 찾을 수 없습니다: {path}") from exc
        except OSError as exc:
            raise CredentialError("자격증명 파일 상태를 안전하게 확인하지 못했습니다.") from exc
        if not stat.S_ISREG(file_stat.st_mode):
            raise CredentialError("자격증명 경로가 일반 파일이 아닙니다.")
        if file_stat.st_uid != os.getuid():
            raise CredentialError("자격증명 파일의 소유자가 현재 사용자와 다릅니다.")
        if stat.S_IMODE(file_stat.st_mode) != 0o600:
            raise CredentialError("자격증명 파일 권한은 정확히 0600이어야 합니다.")

        try:
            raw: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CredentialError("자격증명 파일을 안전하게 읽거나 해석하지 못했습니다.") from exc
        if not isinstance(raw, dict):
            raise CredentialError("자격증명 JSON은 객체여야 합니다.")

        client_id = raw.get("client_id")
        client_secret = raw.get("client_secret")
        if not isinstance(client_id, str) or not isinstance(client_secret, str):
            raise CredentialError("client_id와 client_secret은 문자열이어야 합니다.")
        if not client_id.startswith("tsck_live_"):
            raise CredentialError("client_id 형식이 올바르지 않습니다.")
        if not client_secret.startswith("tssk_live_"):
            raise CredentialError("client_secret 형식이 올바르지 않습니다.")
        if any(char.isspace() for char in client_id + client_secret):
            raise CredentialError("자격증명에 공백이나 줄바꿈이 포함되어 있습니다.")
        return cls(client_id=client_id, client_secret=client_secret)
