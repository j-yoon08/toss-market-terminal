from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from toss_market_terminal.config import CredentialError, Credentials, CredentialStore
from toss_market_terminal.onboarding import (
    credential_status,
    migrate_credentials,
    remove_credentials,
    setup_credentials,
    verify_credentials_read_only,
)

CLIENT_ID = "tsck_live_onboarding_identifier"
CLIENT_SECRET = "tssk_live_onboarding_secret_value"


def prompts(*values: str):
    iterator: Iterator[str] = iter(values)

    def answer(_message: str) -> str:
        return next(iterator)

    return answer


async def accept(_credentials: Credentials) -> None:
    return None


@pytest.mark.asyncio
async def test_setup_saves_after_read_only_verification_without_echoing_secret(
    tmp_path: Path,
) -> None:
    path = tmp_path / "credentials.json"
    output = io.StringIO()
    seen: list[Credentials] = []

    async def verify(credentials: Credentials) -> None:
        seen.append(credentials)

    result = await setup_credentials(
        path,
        input_prompt=prompts(CLIENT_ID),
        secret_prompt=prompts(CLIENT_SECRET),
        verifier=verify,
        output=output,
    )

    assert result.saved is True
    assert result.replaced is False
    assert seen == [Credentials(CLIENT_ID, CLIENT_SECRET)]
    assert CredentialStore(path).load() == Credentials(CLIENT_ID, CLIENT_SECRET)
    rendered = output.getvalue()
    assert "저장 완료" in rendered
    assert CLIENT_ID not in rendered
    assert CLIENT_SECRET not in rendered


@pytest.mark.asyncio
async def test_setup_invalid_input_never_calls_verifier_or_saves(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    called = False

    async def verify(_credentials: Credentials) -> None:
        nonlocal called
        called = True

    with pytest.raises(CredentialError):
        await setup_credentials(
            path,
            input_prompt=prompts("invalid"),
            secret_prompt=prompts(CLIENT_SECRET),
            verifier=verify,
            output=io.StringIO(),
        )
    assert called is False
    assert not path.exists()


@pytest.mark.asyncio
async def test_setup_verification_failure_does_not_save_or_leak(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    output = io.StringIO()

    async def fail(_credentials: Credentials) -> None:
        raise CredentialError("API 인증에 실패했습니다.")

    with pytest.raises(CredentialError) as caught:
        await setup_credentials(
            path,
            input_prompt=prompts(CLIENT_ID),
            secret_prompt=prompts(CLIENT_SECRET),
            verifier=fail,
            output=output,
        )
    assert not path.exists()
    combined = output.getvalue() + str(caught.value)
    assert CLIENT_ID not in combined
    assert CLIENT_SECRET not in combined


@pytest.mark.asyncio
async def test_setup_refuses_existing_file_without_replace(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    CredentialStore(path).save(Credentials.from_values(CLIENT_ID, CLIENT_SECRET))
    with pytest.raises(CredentialError, match="--replace"):
        await setup_credentials(path, verifier=accept, output=io.StringIO())


@pytest.mark.asyncio
async def test_setup_replace_requires_confirmation(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    original = Credentials.from_values(CLIENT_ID, CLIENT_SECRET)
    replacement = Credentials.from_values(
        "tsck_live_replacement_identifier", "tssk_live_replacement_secret_value"
    )
    CredentialStore(path).save(original)

    cancelled = await setup_credentials(
        path,
        replace=True,
        input_prompt=prompts("n"),
        secret_prompt=prompts("unused"),
        verifier=accept,
        output=io.StringIO(),
    )
    assert cancelled.saved is False
    assert CredentialStore(path).load() == original

    replaced = await setup_credentials(
        path,
        replace=True,
        input_prompt=prompts("y", replacement.client_id),
        secret_prompt=prompts(replacement.client_secret),
        verifier=accept,
        output=io.StringIO(),
    )
    assert replaced.saved is True
    assert replaced.replaced is True
    assert CredentialStore(path).load() == replacement


def test_credential_status_is_bounded_and_masked(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    missing = credential_status(path)
    assert missing.configured is False
    assert missing.masked_client_id is None

    CredentialStore(path).save(Credentials.from_values(CLIENT_ID, CLIENT_SECRET))
    configured = credential_status(path)
    assert configured.configured is True
    assert configured.masked_client_id is not None
    assert configured.masked_client_id.endswith(CLIENT_ID[-4:])
    assert CLIENT_ID not in configured.masked_client_id
    assert CLIENT_SECRET not in repr(configured)


def test_remove_requires_confirmation_and_never_prints_secret(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    CredentialStore(path).save(Credentials.from_values(CLIENT_ID, CLIENT_SECRET))
    output = io.StringIO()
    assert remove_credentials(path, input_prompt=prompts("n"), output=output) is False
    assert path.exists()
    assert remove_credentials(path, input_prompt=prompts("y"), output=output) is True
    assert not path.exists()
    assert CLIENT_ID not in output.getvalue()
    assert CLIENT_SECRET not in output.getvalue()


def test_migrate_copies_secure_legacy_without_deleting_it(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.json"
    destination = tmp_path / "new" / "credentials.json"
    original = Credentials.from_values(CLIENT_ID, CLIENT_SECRET)
    CredentialStore(legacy).save(original)
    output = io.StringIO()
    migrate_credentials(legacy, destination, output=output)
    assert CredentialStore(destination).load() == original
    assert CredentialStore(legacy).load() == original
    assert CLIENT_ID not in output.getvalue()
    assert CLIENT_SECRET not in output.getvalue()


@pytest.mark.asyncio
async def test_verify_credentials_calls_only_token_and_read_only_price() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/oauth2/token":
            return httpx.Response(
                200,
                json={"access_token": "test-token", "token_type": "Bearer", "expires_in": 3600},
            )
        assert request.method == "GET"
        assert request.url.path == "/api/v1/prices"
        assert request.headers["authorization"] == "Bearer test-token"
        return httpx.Response(
            200,
            json={
                "result": [
                    {
                        "symbol": "AAPL",
                        "lastPrice": "185.70",
                        "currency": "USD",
                        "timestamp": "2026-08-30T00:00:00Z",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openapi.tossinvest.com"
    ) as http_client:
        await verify_credentials_read_only(
            Credentials.from_values(CLIENT_ID, CLIENT_SECRET), http_client=http_client
        )

    assert seen == [("POST", "/oauth2/token"), ("GET", "/api/v1/prices")]
    assert all(path != "/api/v1/orders" for _, path in seen)
