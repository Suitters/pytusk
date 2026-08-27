#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for the relay POST stage and its retry budget."""

from typing import Any

import pytest
from pysui import SuiRpcResult

from pytusk.commands.relay_commands import RelayUploadAck
from pytusk.core.relay_types import RelayUploadOutcome
from pytusk.core.relay_upload import upload as upload_module
from pytusk.core.relay_upload.upload import upload_to_relay

_RELAY = "https://relay.example.com"


class _FakeRelayClient:
    """Fake ``WalrusClient``-shaped object for ``upload_to_relay``.

    Implements only ``execute(command=..., base_url=...)``, returning the
    next canned response per call and recording every dispatch so the
    resumption tokens sent on each attempt can be compared.
    """

    def __init__(self, *, responses: list[SuiRpcResult]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[Any, str | None]] = []

    async def execute(
        self, *, command: Any, base_url: str | None = None, **kwargs: object
    ) -> SuiRpcResult:
        """Record the dispatch and return the next canned response."""
        self.calls.append((command, base_url))
        return self.responses[len(self.calls) - 1]


def _uploaded(*, certificate: dict | None = None) -> SuiRpcResult:
    """A relay answer that returned a certificate."""
    return SuiRpcResult(
        True,
        "",
        RelayUploadAck(
            outcome=RelayUploadOutcome.UPLOADED,
            blob_id="bid",
            confirmation_certificate=certificate or {"signers": [1]},
            relay_status=200,
            relay_message=None,
        ),
    )


def _answered(*, status: int, body: str = "nope") -> SuiRpcResult:
    """A relay answer that refused, at the given status."""
    return SuiRpcResult(
        False,
        f"HTTP {status}: {body}",
        RelayUploadAck(
            outcome=RelayUploadOutcome.REFUSED,
            blob_id=None,
            confirmation_certificate=None,
            relay_status=status,
            relay_message=body,
        ),
    )


def _transport(*, message: str = "ConnectError on POST url: refused") -> SuiRpcResult:
    """A transport failure: no HTTP response arrived, so there is no ack."""
    return SuiRpcResult(False, message)


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record backoff delays instead of waiting them out."""
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(upload_module.asyncio, "sleep", fake_sleep)
    return delays


async def _upload(client: Any, **kwargs: Any) -> Any:
    """Invoke upload_to_relay with the standard tipped arguments."""
    return await upload_to_relay(
        client=client,
        relay_url=_RELAY,
        blob_id="bid",
        data=b"payload",
        register_tip_tx_digest="0xtx",
        nonce="nonce123",
        **kwargs,
    )


class TestUploadSuccess:
    """The happy path reports UPLOADED and spends one attempt."""

    async def test_first_attempt(self, no_sleep: list[float]) -> None:
        client = _FakeRelayClient(responses=[_uploaded()])
        result = await _upload(client)
        assert result.outcome is RelayUploadOutcome.UPLOADED
        assert result.certificate == {"signers": [1]}
        assert result.attempts == 1
        assert result.transport_error is None
        assert no_sleep == []

    async def test_echoes_resumption_tokens(self, no_sleep: list[float]) -> None:
        client = _FakeRelayClient(responses=[_uploaded()])
        result = await _upload(client)
        assert result.blob_id == "bid"
        assert result.relay_url == _RELAY
        assert result.register_tip_tx_digest == "0xtx"
        assert result.nonce == "nonce123"


class TestUploadRetry:
    """Transport failures and 5xx are retried; the tip is never re-paid."""

    async def test_transport_failure_is_retried(self, no_sleep: list[float]) -> None:
        client = _FakeRelayClient(responses=[_transport(), _uploaded()])
        result = await _upload(client)
        assert result.outcome is RelayUploadOutcome.UPLOADED
        assert result.attempts == 2
        assert len(no_sleep) == 1

    async def test_5xx_is_retried(self, no_sleep: list[float]) -> None:
        client = _FakeRelayClient(responses=[_answered(status=503), _uploaded()])
        result = await _upload(client)
        assert result.outcome is RelayUploadOutcome.UPLOADED
        assert result.attempts == 2

    async def test_backoff_doubles(self, no_sleep: list[float]) -> None:
        client = _FakeRelayClient(
            responses=[_transport(), _transport(), _uploaded()]
        )
        await _upload(client)
        assert no_sleep == [0.5, 1.0]

    async def test_exhausted_budget_is_unanswered(self, no_sleep: list[float]) -> None:
        client = _FakeRelayClient(responses=[_transport()] * 3)
        result = await _upload(client, max_attempts=3)
        assert result.outcome is RelayUploadOutcome.UNANSWERED
        assert result.attempts == 3
        assert result.certificate is None
        assert result.transport_error is not None
        assert len(no_sleep) == 2

    async def test_exhausted_5xx_keeps_status_and_body(
        self, no_sleep: list[float]
    ) -> None:
        client = _FakeRelayClient(responses=[_answered(status=500, body="boom")] * 2)
        result = await _upload(client, max_attempts=2)
        assert result.outcome is RelayUploadOutcome.UNANSWERED
        assert result.relay_status == 500
        assert result.relay_message == "boom"
        assert result.transport_error is None

    async def test_tokens_identical_on_every_attempt(
        self, no_sleep: list[float]
    ) -> None:
        client = _FakeRelayClient(
            responses=[_transport(), _transport(), _uploaded()]
        )
        await _upload(client)
        params = [command.query_params() for command, _ in client.calls]
        assert len(params) == 3
        assert all(entry == params[0] for entry in params)
        assert params[0]["tx_id"] == "0xtx"
        assert params[0]["nonce"] == "nonce123"

    async def test_every_attempt_targets_the_same_relay(
        self, no_sleep: list[float]
    ) -> None:
        client = _FakeRelayClient(responses=[_transport(), _uploaded()])
        await _upload(client)
        assert {url for _, url in client.calls} == {_RELAY}


class TestUploadRefusal:
    """A definitive refusal stops immediately."""

    @pytest.mark.parametrize("status", [400, 401, 402])
    async def test_client_error_is_not_retried(
        self, no_sleep: list[float], status: int
    ) -> None:
        client = _FakeRelayClient(responses=[_answered(status=status, body="denied")])
        result = await _upload(client)
        assert result.outcome is RelayUploadOutcome.REFUSED
        assert result.attempts == 1
        assert result.relay_status == status
        assert result.relay_message == "denied"
        assert no_sleep == []

    async def test_refusal_carries_no_certificate(
        self, no_sleep: list[float]
    ) -> None:
        client = _FakeRelayClient(responses=[_answered(status=400)])
        result = await _upload(client)
        assert result.certificate is None


class TestUploadContract:
    """Caller-side contract violations raise before any request."""

    async def test_zero_attempts_rejected(self) -> None:
        client = _FakeRelayClient(responses=[])
        with pytest.raises(ValueError, match="max_attempts must be at least 1"):
            await _upload(client, max_attempts=0)
