#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for the shared Tx2 stage, ``submit_certification``.

The stage is exercised with ``execute_certify`` and ``verify_certificate``
monkeypatched on :mod:`pytusk.core.ops.blob_execute` -- the same boundary
``test_native_upload.py`` fakes, and for the same reason: everything below
it needs a live node, while the local verification, exception conversion and
timing under test here do not.
"""

import types

import pytest

from pytusk.core.ops import blob_execute as ops_blob_execute
from pytusk.core.ops.blob_execute import CertificationOutcome, submit_certification
from pytusk.core.types import CertifyResult, Registration
from pytusk.core.types.errors import CertifyTransactionError


def _registration() -> Registration:
    """Build a Registration standing in for Tx1's result."""
    return Registration(
        object_id="0xblob",
        blob_id=b"\x22" * 32,
        end_epoch=11,
        deletable=False,
        digest="0xtx1",
    )


def _committee(*, signer_count: int = 3) -> types.SimpleNamespace:
    """Stand in for a committee; only member public keys are read."""
    return types.SimpleNamespace(
        epoch=5,
        members=[
            types.SimpleNamespace(public_key=bytes([index]) * 96)
            for index in range(signer_count)
        ],
    )


def _certificate(*, positions: tuple[int, ...] = (0, 1)) -> types.SimpleNamespace:
    """Stand in for a Certificate; only signer_positions is read here."""
    return types.SimpleNamespace(signer_positions=positions)


def _certify_result() -> CertifyResult:
    """Build the CertifyResult a successful Tx2 returns."""
    return CertifyResult(
        object_id="0xblob",
        blob_id=b"\x22" * 32,
        certified=True,
        digest="0xtx2",
    )


async def _submit(**overrides: object) -> CertificationOutcome:
    """Call submit_certification with the standard fixtures."""
    kwargs = {
        "client": object(),
        "committee": _committee(),
        "registration": _registration(),
        "certificate": _certificate(),
        "package_id": "0xpkg",
        "system_object": "0xsystem",
    }
    kwargs.update(overrides)
    return await submit_certification(**kwargs)


class TestSubmitCertificationSuccess:
    """The happy path returns Tx2's result and its duration."""

    async def test_returns_result_and_duration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A successful submit yields the CertifyResult and a duration."""
        result = _certify_result()

        async def fake_execute_certify(**kwargs: object) -> CertifyResult:
            return result

        monkeypatch.setattr(ops_blob_execute, "execute_certify", fake_execute_certify)
        monkeypatch.setattr(
            ops_blob_execute, "verify_certificate", lambda **kwargs: True
        )

        outcome = await _submit()

        assert isinstance(outcome, CertificationOutcome)
        assert outcome.result is result
        assert outcome.duration >= 0.0

    async def test_builds_no_receipt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The stage is receipt-free -- it returns transaction facts only."""

        async def fake_execute_certify(**kwargs: object) -> CertifyResult:
            return _certify_result()

        monkeypatch.setattr(ops_blob_execute, "execute_certify", fake_execute_certify)
        monkeypatch.setattr(
            ops_blob_execute, "verify_certificate", lambda **kwargs: True
        )

        outcome = await _submit()

        assert not hasattr(outcome, "timings")
        assert not hasattr(outcome, "failed_stage")
        assert not hasattr(outcome, "certified")

    async def test_threads_signing_arguments_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """sender, sponsor and recipient reach execute_certify unchanged."""
        seen: dict = {}

        async def fake_execute_certify(**kwargs: object) -> CertifyResult:
            seen.update(kwargs)
            return _certify_result()

        monkeypatch.setattr(ops_blob_execute, "execute_certify", fake_execute_certify)
        monkeypatch.setattr(
            ops_blob_execute, "verify_certificate", lambda **kwargs: True
        )

        await _submit(sender="0xsender", sponsor="0xsponsor", recipient="0xrecipient")

        assert seen["sender"] == "0xsender"
        assert seen["sponsor"] == "0xsponsor"
        assert seen["recipient"] == "0xrecipient"
        assert seen["package_id"] == "0xpkg"
        assert seen["system_object"] == "0xsystem"


class TestSubmitCertificationLocalVerification:
    """A certificate that does not verify never reaches the chain."""

    async def test_verification_failure_raises_before_submitting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing is submitted, and duration stays None -- no gas spent."""
        submitted = False

        async def fake_execute_certify(**kwargs: object) -> CertifyResult:
            nonlocal submitted
            submitted = True
            return _certify_result()

        monkeypatch.setattr(ops_blob_execute, "execute_certify", fake_execute_certify)
        monkeypatch.setattr(
            ops_blob_execute, "verify_certificate", lambda **kwargs: False
        )

        with pytest.raises(CertifyTransactionError) as excinfo:
            await _submit()

        assert submitted is False
        assert excinfo.value.stage == "certify"
        assert excinfo.value.duration is None
        assert "does not verify" in str(excinfo.value)

    async def test_signer_public_keys_looked_up_by_position(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Public keys are taken from the committee at the signer positions."""
        seen: dict = {}

        def fake_verify(**kwargs: object) -> bool:
            seen.update(kwargs)
            return True

        async def fake_execute_certify(**kwargs: object) -> CertifyResult:
            return _certify_result()

        monkeypatch.setattr(ops_blob_execute, "execute_certify", fake_execute_certify)
        monkeypatch.setattr(ops_blob_execute, "verify_certificate", fake_verify)

        committee = _committee(signer_count=4)
        await _submit(committee=committee, certificate=_certificate(positions=(1, 3)))

        assert seen["public_keys"] == [
            committee.members[1].public_key,
            committee.members[3].public_key,
        ]


class TestSubmitCertificationErrorConversion:
    """Both bare exception types pysui can raise become one typed error."""

    async def test_runtimeerror_becomes_certify_transaction_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A submission failure or on-chain abort is converted, with timing."""
        original = "certify_blob transaction aborted on-chain: EWrongEpoch"

        async def fake_execute_certify(**kwargs: object) -> CertifyResult:
            raise RuntimeError(original)

        monkeypatch.setattr(ops_blob_execute, "execute_certify", fake_execute_certify)
        monkeypatch.setattr(
            ops_blob_execute, "verify_certificate", lambda **kwargs: True
        )

        with pytest.raises(CertifyTransactionError) as excinfo:
            await _submit()

        assert excinfo.value.stage == "certify"
        assert original in str(excinfo.value)
        assert excinfo.value.duration is not None
        assert excinfo.value.duration >= 0
        assert isinstance(excinfo.value.__cause__, RuntimeError)
        assert str(excinfo.value.__cause__) == original

    async def test_valueerror_becomes_certify_transaction_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pysui signals a simulate failure with ValueError, not RuntimeError.

        Guards the same defect ``test_native_upload.py`` pins at the
        ``certify()`` level, now at the layer that actually does the
        conversion.
        """
        original = "Error running SimulateTransactionKind: boom"

        async def fake_execute_certify(**kwargs: object) -> CertifyResult:
            raise ValueError(original)

        monkeypatch.setattr(ops_blob_execute, "execute_certify", fake_execute_certify)
        monkeypatch.setattr(
            ops_blob_execute, "verify_certificate", lambda **kwargs: True
        )

        with pytest.raises(CertifyTransactionError) as excinfo:
            await _submit()

        assert excinfo.value.stage == "certify"
        assert original in str(excinfo.value)
        assert excinfo.value.duration is not None
        assert excinfo.value.duration >= 0
        assert isinstance(excinfo.value.__cause__, ValueError)
        assert str(excinfo.value.__cause__) == original
