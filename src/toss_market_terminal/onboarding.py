from __future__ import annotations

import getpass
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import httpx

from .client import TossMarketClient
from .config import CredentialError, Credentials, CredentialStore, mask_client_id

InputPrompt = Callable[[str], str]
CredentialVerifier = Callable[[Credentials], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class SetupResult:
    saved: bool
    replaced: bool
    path: Path


@dataclass(frozen=True, slots=True)
class CredentialStatus:
    configured: bool
    path: Path
    masked_client_id: str | None
    storage: str = "private-file-0600"


async def verify_credentials_read_only(
    credentials: Credentials,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    """Verify OAuth plus one allowlisted public price GET; never touch orders/accounts."""
    try:
        async with TossMarketClient(credentials, http_client=http_client) as client:
            await client.price("AAPL")
    except httpx.HTTPError as exc:
        raise CredentialError("토스 Open API에 안전하게 연결하지 못했습니다.") from exc


async def setup_credentials(
    path: Path,
    *,
    replace: bool = False,
    input_prompt: InputPrompt | None = None,
    secret_prompt: InputPrompt | None = None,
    verifier: CredentialVerifier = verify_credentials_read_only,
    output: TextIO | None = None,
) -> SetupResult:
    output = output or sys.stdout
    input_prompt = input_prompt or input
    secret_prompt = secret_prompt or getpass.getpass
    store = CredentialStore(path)
    existing = store.exists()
    if existing and not replace:
        raise CredentialError(
            "자격증명이 이미 설정되어 있습니다. 교체하려면 --replace를 사용하세요."
        )
    if existing:
        try:
            answer = input_prompt("기존 자격증명을 새 값으로 교체하시겠습니까? [y/N] ")
        except EOFError as exc:
            raise CredentialError("대화형 입력을 완료하지 못했습니다.") from exc
        if answer.strip().lower() not in {"y", "yes"}:
            print("자격증명 교체를 취소했습니다.", file=output)
            return SetupResult(saved=False, replaced=False, path=store.path)

    print("Toss Market Terminal 최초 연결 설정", file=output)
    print("Client Secret은 화면에 표시되지 않으며 이 컴퓨터의 0600 파일에 저장됩니다.", file=output)
    print("PAPER가 기본이며 이 설정은 LIVE 주문을 활성화하지 않습니다.", file=output)
    try:
        client_id = input_prompt("Client ID: ")
        client_secret = secret_prompt("Client Secret: ")
    except EOFError as exc:
        raise CredentialError("대화형 입력을 완료하지 못했습니다.") from exc
    credentials = Credentials.from_values(client_id, client_secret)

    print("읽기 전용 API 연결을 확인하는 중입니다...", file=output)
    await verifier(credentials)
    store.save(credentials, replace=existing)
    print("API 연결 확인 및 자격증명 저장 완료", file=output)
    print(f"저장 위치: {store.path}", file=output)
    return SetupResult(saved=True, replaced=existing, path=store.path)


def credential_status(path: Path) -> CredentialStatus:
    store = CredentialStore(path)
    if not store.exists():
        return CredentialStatus(configured=False, path=store.path, masked_client_id=None)
    credentials = store.load()
    return CredentialStatus(
        configured=True,
        path=store.path,
        masked_client_id=mask_client_id(credentials.client_id),
    )


def remove_credentials(
    path: Path,
    *,
    input_prompt: InputPrompt | None = None,
    output: TextIO | None = None,
) -> bool:
    output = output or sys.stdout
    input_prompt = input_prompt or input
    store = CredentialStore(path)
    if not store.exists():
        print("삭제할 자격증명이 없습니다.", file=output)
        return False
    try:
        answer = input_prompt("저장된 Toss Open API 자격증명을 삭제하시겠습니까? [y/N] ")
    except EOFError as exc:
        raise CredentialError("대화형 입력을 완료하지 못했습니다.") from exc
    if answer.strip().lower() not in {"y", "yes"}:
        print("자격증명 삭제를 취소했습니다.", file=output)
        return False
    store.remove()
    print("자격증명을 삭제했습니다.", file=output)
    return True


def migrate_credentials(
    legacy_path: Path,
    destination_path: Path,
    *,
    output: TextIO | None = None,
) -> Path:
    output = output or sys.stdout
    legacy = CredentialStore(legacy_path)
    destination = CredentialStore(destination_path)
    if not legacy.exists():
        raise CredentialError("마이그레이션할 기존 자격증명 파일이 없습니다.")
    if destination.exists():
        raise CredentialError("새 자격증명 경로가 이미 설정되어 있습니다.")
    destination.save(legacy.load())
    print("기존 자격증명을 새 경로로 안전하게 복사했습니다.", file=output)
    print("기존 파일은 삭제하지 않았습니다.", file=output)
    print(f"새 저장 위치: {destination.path}", file=output)
    return destination.path
