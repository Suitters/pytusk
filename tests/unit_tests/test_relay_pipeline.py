#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for the encapsulated store_blob_relay pipeline."""

import types
from typing import Any

import pytest

from pytusk.core.ops import blob_execute as ops_blob_execute
from pytusk.core.pipelines import delivery as delivery_module
from pytusk.core.pipelines import registration as registration_module
from pytusk.core.pipelines import write as pipeline_module
from pytusk.core.pipelines.write import store_blob_relay
from pytusk.core.relay_upload.common import TipQuote
from pytusk.core.types import (
    FROM_GAS,
    ConstTip,
    RegistrationPendingError,
    RelayCertificateParseError,
    RelayOutcome,
    RelayUploadError,
    RelayUploadOutcome,
    RelayUploadResult,
)

_RELAY_URL = "https://relay.example"


class _FakeTusk:
    """PytuskConfiguration stub."""

    def __init__(self) -> None:
        self.active_network = "testnet"
        self.network = types.SimpleNamespace(system_object="0xsys")
        self.relay_calls: list[tuple[str, str | None]] = []

    def relay_url_for(self, *, network_name: str, relay_name: str | None = None) -> str:
        """Record the resolution request and return a fixed URL."""
        self.relay_calls.append((network_name, relay_name))
        return _RELAY_URL


class _FakePysuiConfig:
    """PysuiConfiguration stub with a controllable signable set."""

    def __init__(self, *, signable: tuple[str, ...]) -> None:
        self.active_address = "0xsender"
        self._signable = set(signable)

    def keypair_for_address(self, *, address: str) -> object:
        """Mirror pysui: raise ValueError when the address has no key."""
        if address not in self._signable:
            raise ValueError(f"Keypair for address: {address} does not exist.")
        return object()


class _FakeTxn:
    """Transaction stub recording only what the pipeline drives."""

    def __init__(self) -> None:
        self.transfers: list[tuple[list[Any], str]] = []

    async def transfer_objects(self, *, transfers: list[Any], recipient: str) -> None:
        """Record the transfer."""
        self.transfers.append((transfers, recipient))


class _FakeClient:
    """WalrusClient stub."""

    def __init__(self, *, signable: tuple[str, ...] = ()) -> None:
        self.config = _FakeTusk()
        self.pysui_client = types.SimpleNamespace(
            config=_FakePysuiConfig(signable=signable)
        )
        self.txn = _FakeTxn()
        self.txn_kwargs: dict = {}

    async def committee(self) -> types.SimpleNamespace:
        """Return a committee with a fixed shard count."""
        return types.SimpleNamespace(n_shards=1000)

    async def transaction(self, **kwargs: Any) -> _FakeTxn:
        """Record how the transaction was opened."""
        self.txn_kwargs = kwargs
        return self.txn


class _Recorder:
    """Captures every delegated call the pipeline makes."""

    def __init__(self) -> None:
        self.order: list[str] = []
        self.add_tip: list[dict] = []
        self.register: list[dict] = []
        self.preflight_sponsor_calls: list[dict] = []
        self.preflight_payment_calls: list[dict] = []
        self.upload: list[dict] = []
        self.certify: list[dict] = []
        self.coin_checks: list[dict] = []


def _upload_result(
    *,
    outcome: RelayUploadOutcome = RelayUploadOutcome.UPLOADED,
    certificate: dict | None = None,
) -> RelayUploadResult:
    """Build a RelayUploadResult with the given outcome."""
    return RelayUploadResult(
        outcome=outcome,
        certificate={"signers": [0]} if certificate is None else certificate,
        blob_id="BLOBID",
        relay_url=_RELAY_URL,
        register_tip_tx_digest="0xtx1",
        nonce="NONCE",
        relay_status=200,
        relay_message="ok",
        transport_error=None,
        attempts=1,
        duration=0.25,
    )


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """Patch every collaborator of the pipeline with a recording stub."""
    recorder = _Recorder()

    def _encode(*, data: bytes, n_shards: int) -> types.SimpleNamespace:
        recorder.order.append("encode")
        return types.SimpleNamespace(blob_id=b"\x01" * 32, blob_id_base64="BLOBID")

    async def _quote(**kwargs: Any) -> TipQuote:
        recorder.order.append("quote")
        return TipQuote(address="0xrelay", amount=1000, kind=ConstTip(amount=1000))

    async def _add_registration_sequence(**kwargs: Any) -> None:
        tip = kwargs.get("tip")
        if tip is not None:
            recorder.order.append("add_tip")
            recorder.add_tip.append(
                {
                    "relay_address": tip.relay_address,
                    "tip_amount": tip.tip_amount,
                    "auth_package": tip.auth_package,
                    "payment_coin": tip.payment_coin,
                }
            )
        recorder.order.append("register")
        recorder.register.append(kwargs)

    async def _preflight_sponsor(**kwargs: Any) -> None:
        recorder.order.append("preflight_sponsor")
        recorder.preflight_sponsor_calls.append(kwargs)
        sponsor = kwargs.get("sponsor")
        if sponsor is not None:
            try:
                kwargs["client"].pysui_client.config.keypair_for_address(
                    address=sponsor
                )
            except ValueError as exc:
                raise RelayUploadError(
                    message=(
                        f"Sponsor {sponsor} is not signable in the active "
                        "PysuiConfiguration. The encapsulated relay pipeline "
                        "must be able to sign the certify transaction, which "
                        "cannot be pre-built for external signing. Use the "
                        "composable relay functions for external sponsors."
                    ),
                    stage="preflight",
                ) from exc

    async def _preflight_payment(**kwargs: Any) -> str:
        recorder.order.append("preflight_payment")
        recorder.preflight_payment_calls.append(kwargs)
        sponsor = kwargs.get("sponsor")
        tip_source = kwargs.get("tip_source")
        if tip_source is not None and tip_source != FROM_GAS:
            owners = {kwargs["sender"]}
            if sponsor is not None:
                owners.add(sponsor)
            recorder.coin_checks.append(
                {
                    "coin_id": tip_source,
                    "owners": owners,
                    "minimum_balance": kwargs.get("tip_minimum_balance", 0),
                }
            )
        return kwargs.get("wal_payment_coin") or "0xwal"

    async def _exec_reg(**kwargs: Any) -> types.SimpleNamespace:
        recorder.order.append("exec_reg")
        return types.SimpleNamespace(
            object_id="0xblob",
            blob_id=b"\x01" * 32,
            end_epoch=42,
            # Mirrors the real read-back rather than hard-coding False. The
            # created Blob's deletable flag reflects what Tx1 ACTUALLY
            # registered, which is what add_registration_sequence was told to
            # compose -- execute_registration_txn itself is never passed the
            # flag. The relay is told the blob object id based on this
            # chain-read value, not on the caller's request, so a fixture
            # that never varied it would let the two silently disagree.
            deletable=(
                recorder.register[-1]["deletable"] if recorder.register else False
            ),
            digest="0xtx1",
        )

    async def _exec_plain_reg(**kwargs: Any) -> types.SimpleNamespace:
        # The no-tip relay write registers through PlainBlobRegistration,
        # which reaches execute_reserve_and_register -- a single call that
        # both composes and submits -- rather than the
        # add_registration_sequence / execute_registration_txn pair the
        # tipped path uses. It is recorded as the same "register" step so
        # assertions about what Tx1 registered need not know which
        # registration variant ran.
        recorder.order.append("register")
        recorder.register.append(kwargs)
        return await _exec_reg(**kwargs)

    async def _upload(**kwargs: Any) -> RelayUploadResult:
        recorder.order.append("upload")
        recorder.upload.append(kwargs)
        return _upload_result()

    def _parse(**kwargs: Any) -> object:
        recorder.order.append("parse")
        # Shaped like a real Certificate rather than a bare object: the
        # shared Tx2 stage reads signer_positions to look up the signers'
        # public keys for its local verification. Empty means no lookup,
        # which keeps this fixture free of a committee member list.
        return types.SimpleNamespace(signer_positions=())

    async def _certify(**kwargs: Any) -> types.SimpleNamespace:
        recorder.order.append("certify")
        recorder.certify.append(kwargs)
        return types.SimpleNamespace(
            object_id="0xblob", blob_id=b"", certified=True, digest="0xtx2"
        )

    async def _resolve_pkg(**kwargs: Any) -> str:
        return "0xpkg"

    monkeypatch.setattr(pipeline_module, "encode_blob", _encode)
    monkeypatch.setattr(pipeline_module, "quote_tip", _quote)
    monkeypatch.setattr(
        registration_module, "add_registration_sequence", _add_registration_sequence
    )
    monkeypatch.setattr(pipeline_module, "preflight_sponsor", _preflight_sponsor)
    monkeypatch.setattr(pipeline_module, "preflight_payment", _preflight_payment)
    monkeypatch.setattr(registration_module, "execute_registration_txn", _exec_reg)
    # BOTH registration variants are stubbed. The tipped path reaches the
    # compose/execute pair above; the no-tip path reaches
    # execute_reserve_and_register below. Patching only one would let a test
    # that switches paths silently run real chain code against a fake client.
    monkeypatch.setattr(
        registration_module, "execute_reserve_and_register", _exec_plain_reg
    )
    monkeypatch.setattr(delivery_module, "upload_to_relay", _upload)
    monkeypatch.setattr(delivery_module, "parse_relay_certificate", _parse)
    monkeypatch.setattr(ops_blob_execute, "execute_certify", _certify)
    monkeypatch.setattr(
        ops_blob_execute, "verify_certificate", lambda **kwargs: True
    )
    monkeypatch.setattr(pipeline_module, "resolve_package_id", _resolve_pkg)
    return recorder


class TestSponsorPrecondition:
    """An unsignable sponsor is refused before Tx1 is opened -- and before
    ANY encode or network work, restoring the original fail-fast ordering.

    The check runs via the standalone ``preflight_sponsor``
    (``pytusk.core.ops.blob_execute``), called FIRST in the pipeline --
    before ``encode``, ``quote_tip``, or anything else -- unlike the
    tip-coin/WAL-coin resolution (``preflight_payment``), which genuinely
    needs the tip quote and so cannot run this early. See
    ``preflight_sponsor``'s docstring. What is guaranteed here is that
    NOTHING is ever built, encoded, quoted, or spent: ``encode_blob`` and
    ``quote_tip`` are never called, ``client.transaction`` is never called,
    and no PTB command is ever composed.
    """

    async def test_unsignable_sponsor_raises(self, rec: _Recorder) -> None:
        client = _FakeClient()
        with pytest.raises(RelayUploadError, match="not signable"):
            await store_blob_relay(
                client=client, data=b"x", epochs=1, sponsor="0xstranger"
            )

    async def test_unsignable_sponsor_reports_preflight_stage(
        self, rec: _Recorder
    ) -> None:
        client = _FakeClient()
        with pytest.raises(RelayUploadError) as excinfo:
            await store_blob_relay(
                client=client, data=b"x", epochs=1, sponsor="0xstranger"
            )
        assert excinfo.value.stage == "preflight"

    async def test_unsignable_sponsor_builds_nothing(self, rec: _Recorder) -> None:
        """Fail-fast regression guard: the sponsor check must be the VERY
        FIRST thing the pipeline does. If a future change ever moves it
        after ``encode``/``quote`` again, ``rec.order`` will contain more
        than just ``preflight_sponsor`` and this assertion catches it."""
        client = _FakeClient()
        with pytest.raises(RelayUploadError):
            await store_blob_relay(
                client=client, data=b"x", epochs=1, sponsor="0xstranger"
            )
        assert rec.order == ["preflight_sponsor"]
        assert client.txn_kwargs == {}

    async def test_signable_sponsor_proceeds(self, rec: _Recorder) -> None:
        client = _FakeClient(signable=("0xsponsor",))
        receipt = await store_blob_relay(
            client=client, data=b"x", epochs=1, sponsor="0xsponsor"
        )
        assert receipt.outcome is RelayOutcome.CERTIFIED
        assert client.txn_kwargs["initial_sponsor"] == "0xsponsor"


class TestTipBundling:
    """The tip and the registration share one transaction, tip first."""

    async def test_tip_is_composed_before_registration(self, rec: _Recorder) -> None:
        client = _FakeClient()
        await store_blob_relay(client=client, data=b"x", epochs=1)
        assert rec.order.index("add_tip") < rec.order.index("register")

    async def test_tip_uses_quoted_address_and_amount(self, rec: _Recorder) -> None:
        client = _FakeClient()
        await store_blob_relay(client=client, data=b"x", epochs=1)
        assert rec.add_tip[0]["relay_address"] == "0xrelay"
        assert rec.add_tip[0]["tip_amount"] == 1000

    async def test_tip_auth_package_is_seventy_two_bytes(
        self, rec: _Recorder
    ) -> None:
        client = _FakeClient()
        await store_blob_relay(client=client, data=b"x", epochs=1)
        assert len(rec.add_tip[0]["auth_package"].bcs) == 72

    async def test_upload_carries_digest_and_nonce_when_tipped(
        self, rec: _Recorder
    ) -> None:
        client = _FakeClient()
        await store_blob_relay(client=client, data=b"x", epochs=1)
        assert rec.upload[0]["register_tip_tx_digest"] == "0xtx1"
        assert rec.upload[0]["nonce"] is not None

    async def test_free_relay_skips_the_tip(
        self, rec: _Recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _free(**kwargs: Any) -> TipQuote:
            rec.order.append("quote")
            return TipQuote(address=None, amount=None, kind=None)

        monkeypatch.setattr(pipeline_module, "quote_tip", _free)
        client = _FakeClient()
        receipt = await store_blob_relay(client=client, data=b"x", epochs=1)
        assert rec.add_tip == []
        assert receipt.tip_paid is False
        assert rec.upload[0]["nonce"] is None
        assert rec.upload[0]["register_tip_tx_digest"] is None


class TestCoinVerification:
    """An explicit tip coin is verified against the paying parties."""

    async def test_from_gas_skips_the_check(self, rec: _Recorder) -> None:
        client = _FakeClient()
        await store_blob_relay(client=client, data=b"x", epochs=1)
        assert rec.coin_checks == []

    async def test_explicit_coin_is_checked_against_sender(
        self, rec: _Recorder
    ) -> None:
        client = _FakeClient()
        await store_blob_relay(
            client=client, data=b"x", epochs=1, tip_source="0xcoin"
        )
        assert rec.coin_checks[0]["coin_id"] == "0xcoin"
        assert rec.coin_checks[0]["owners"] == {"0xsender"}

    async def test_explicit_coin_check_includes_sponsor(
        self, rec: _Recorder
    ) -> None:
        client = _FakeClient(signable=("0xsponsor",))
        await store_blob_relay(
            client=client,
            data=b"x",
            epochs=1,
            tip_source="0xcoin",
            sponsor="0xsponsor",
        )
        assert rec.coin_checks[0]["owners"] == {"0xsender", "0xsponsor"}

    async def test_explicit_coin_requires_the_tip_amount(
        self, rec: _Recorder
    ) -> None:
        client = _FakeClient()
        await store_blob_relay(
            client=client, data=b"x", epochs=1, tip_source="0xcoin"
        )
        assert rec.coin_checks[0]["minimum_balance"] == 1000


class TestOutcomeMapping:
    """Every relay outcome maps to the right receipt discriminator."""

    async def test_uploaded_certifies(self, rec: _Recorder) -> None:
        client = _FakeClient()
        receipt = await store_blob_relay(client=client, data=b"x", epochs=1)
        assert receipt.outcome is RelayOutcome.CERTIFIED
        assert receipt.certified is True
        assert receipt.certify_tx_digest == "0xtx2"
        assert receipt.failed_stage is None

    async def test_refused_is_rejected(
        self, rec: _Recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _refused(**kwargs: Any) -> RelayUploadResult:
            rec.upload.append(kwargs)
            return _upload_result(
                outcome=RelayUploadOutcome.REFUSED, certificate=None
            )

        monkeypatch.setattr(delivery_module, "upload_to_relay", _refused)
        client = _FakeClient()
        receipt = await store_blob_relay(client=client, data=b"x", epochs=1)
        assert receipt.outcome is RelayOutcome.REJECTED
        assert receipt.failed_stage == "relay_upload"

    async def test_unanswered_is_resumable(
        self, rec: _Recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _unanswered(**kwargs: Any) -> RelayUploadResult:
            rec.upload.append(kwargs)
            return _upload_result(
                outcome=RelayUploadOutcome.UNANSWERED, certificate=None
            )

        monkeypatch.setattr(delivery_module, "upload_to_relay", _unanswered)
        client = _FakeClient()
        receipt = await store_blob_relay(client=client, data=b"x", epochs=1)
        assert receipt.outcome is RelayOutcome.RESUMABLE

    async def test_unanswered_carries_resumption_tokens(
        self, rec: _Recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _unanswered(**kwargs: Any) -> RelayUploadResult:
            rec.upload.append(kwargs)
            return _upload_result(
                outcome=RelayUploadOutcome.UNANSWERED, certificate=None
            )

        monkeypatch.setattr(delivery_module, "upload_to_relay", _unanswered)
        client = _FakeClient()
        receipt = await store_blob_relay(client=client, data=b"x", epochs=1)
        assert receipt.register_tx_digest == "0xtx1"
        assert receipt.blob_id == "BLOBID"
        assert receipt.nonce is not None


class TestRegistrationPendingErrorConvertedToReceipt:
    """A post-spend ``RegistrationPendingError`` from
    ``execute_registration_txn`` must reach the caller as RETURN DATA, not
    as a raise -- converted at the pipeline boundary
    (``pytusk/core/pipelines/write.py``) into a ``RelayBlobReceipt``
    with ``outcome=RelayOutcome.RESUMABLE``."""

    async def _run_with_pending_error(
        self, rec: _Recorder, monkeypatch: pytest.MonkeyPatch, stage: str
    ) -> object:
        async def _pending(**kwargs: Any) -> None:
            raise RegistrationPendingError(
                digest="0xtx1",
                object_id="0xblob",
                detail="boom",
                stage=stage,
            )

        monkeypatch.setattr(registration_module, "execute_registration_txn", _pending)
        client = _FakeClient()
        return await store_blob_relay(client=client, data=b"x", epochs=1)

    async def test_register_finality_stage_is_resumable(
        self, rec: _Recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A finality-wait timeout must return a RESUMABLE receipt, not raise."""
        receipt = await self._run_with_pending_error(rec, monkeypatch, "register_finality")

        assert receipt.outcome is RelayOutcome.RESUMABLE
        assert receipt.certified is False
        assert receipt.failed_stage == "register_finality"
        assert receipt.register_tx_digest == "0xtx1"
        assert receipt.object_id == "0xblob"
        assert receipt.end_epoch is None

    async def test_register_readback_stage_is_resumable(
        self, rec: _Recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A read-back failure (after finality landed) must return a
        RESUMABLE receipt, not raise."""
        receipt = await self._run_with_pending_error(rec, monkeypatch, "register_readback")

        assert receipt.outcome is RelayOutcome.RESUMABLE
        assert receipt.certified is False
        assert receipt.failed_stage == "register_readback"
        assert receipt.register_tx_digest == "0xtx1"
        assert receipt.object_id == "0xblob"
        assert receipt.end_epoch is None


class TestCertifyFailure:
    """A failure after the tip is spent reports, never raises."""

    async def test_certify_error_is_resumable(
        self, rec: _Recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _boom(**kwargs: Any) -> None:
            raise RuntimeError("certify exploded")

        monkeypatch.setattr(ops_blob_execute, "execute_certify", _boom)
        client = _FakeClient()
        receipt = await store_blob_relay(client=client, data=b"x", epochs=1)
        assert receipt.outcome is RelayOutcome.RESUMABLE
        assert receipt.failed_stage == "certify"
        assert receipt.certified is False

    async def test_certify_error_preserves_tokens(
        self, rec: _Recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _boom(**kwargs: Any) -> None:
            raise RuntimeError("certify exploded")

        monkeypatch.setattr(ops_blob_execute, "execute_certify", _boom)
        client = _FakeClient()
        receipt = await store_blob_relay(client=client, data=b"x", epochs=1)
        assert receipt.register_tx_digest == "0xtx1"
        assert receipt.nonce is not None
        assert receipt.tip_paid is True

    async def test_certificate_parse_error_is_resumable(
        self, rec: _Recorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _bad(**kwargs: Any) -> None:
            raise RelayCertificateParseError(
                message="bad certificate", stage="parse_certificate"
            )

        monkeypatch.setattr(delivery_module, "parse_relay_certificate", _bad)
        client = _FakeClient()
        receipt = await store_blob_relay(client=client, data=b"x", epochs=1)
        assert receipt.outcome is RelayOutcome.RESUMABLE
        assert receipt.object_id == "0xblob"


class TestDeletable:
    """The deletable blob object is sent only for deletable blobs.

    The object id follows what Tx1 ACTUALLY registered -- the ``deletable``
    flag read back from the created ``Blob`` -- rather than the caller's
    request. The two agree in practice; where they could not, the relay must
    be told what is on chain, because that is what it re-verifies against.
    """

    async def test_deletable_sends_object_id(self, rec: _Recorder) -> None:
        client = _FakeClient()
        await store_blob_relay(client=client, data=b"x", epochs=1, deletable=True)
        assert rec.upload[0]["deletable_blob_object"] == "0xblob"

    async def test_permanent_sends_none(self, rec: _Recorder) -> None:
        client = _FakeClient()
        await store_blob_relay(client=client, data=b"x", epochs=1, deletable=False)
        assert rec.upload[0]["deletable_blob_object"] is None


class TestRelayResolution:
    """The relay URL is resolved from config, honouring an explicit name."""

    async def test_uses_active_network_and_named_relay(self, rec: _Recorder) -> None:
        client = _FakeClient()
        await store_blob_relay(
            client=client, data=b"x", epochs=1, relay_name="mysten"
        )
        assert client.config.relay_calls == [("testnet", "mysten")]

    async def test_defaults_relay_name_to_none(self, rec: _Recorder) -> None:
        client = _FakeClient()
        await store_blob_relay(client=client, data=b"x", epochs=1)
        assert client.config.relay_calls == [("testnet", None)]
