#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for the delivery seam.

These exercise the two adapters in isolation, with the underlying stage
functions patched out. Neither pipeline is wired to this seam yet, so the
point of these tests is that each adapter passes exactly the arguments the
live pipeline passes today, and maps outcomes without inventing behaviour.
"""

import types

import pytest

from pytusk.core.certification import Certificate
from pytusk.core.pipelines import delivery as delivery_module
from pytusk.core.pipelines.delivery import (
    BlobDelivery,
    DeliveryOutcome,
    DeliveryResult,
    NativeDelivery,
    NativeDeliveryResult,
    RelayDelivery,
    RelayDeliveryResult,
)
from pytusk.core.types import (
    ConfirmationCollectionError,
    Registration,
    RelayCertificateParseError,
    RelayUploadOutcome,
    SliverUploadError,
)


def _registration(*, deletable: bool = False) -> Registration:
    """Build a Registration standing in for Tx1's result."""
    return Registration(
        object_id="0x" + "ab" * 32,
        blob_id=b"\x11" * 32,
        end_epoch=42,
        deletable=deletable,
        digest="TX1DIGEST",
    )


def _certificate() -> Certificate:
    """Build a minimal certificate."""
    return Certificate(
        serialized_message=b"message",
        aggregate_signature=b"signature",
        signers_bitmap=b"\x01",
        signer_positions=(0,),
        weight=7,
    )


def _encoded() -> types.SimpleNamespace:
    """Stand in for an EncodedBlob, which the adapters only read IDs from."""
    return types.SimpleNamespace(
        blob_id=b"\x11" * 32,
        blob_id_base64="EREREREREREREREREREREREREREREREREREREREREQ",
    )


def _upload_result(
    *,
    outcome: RelayUploadOutcome,
    certificate: dict | None = None,
) -> types.SimpleNamespace:
    """Stand in for a RelayUploadResult from the POST stage."""
    return types.SimpleNamespace(
        outcome=outcome,
        certificate=certificate,
        duration=0.25,
        relay_status=200,
        relay_message=None,
        attempts=1,
        transport_error=None,
    )


class TestProtocolConformance:
    """Both adapters must satisfy the declared seam."""

    def test_native_delivery_satisfies_protocol(self):
        """NativeDelivery is a BlobDelivery."""
        assert isinstance(NativeDelivery(), BlobDelivery)

    def test_relay_delivery_satisfies_protocol(self):
        """RelayDelivery is a BlobDelivery."""
        assert isinstance(RelayDelivery(relay_url="https://relay.example"), BlobDelivery)

    def test_native_result_is_a_delivery_result(self):
        """The native subclass satisfies the declared return type."""
        assert issubclass(NativeDeliveryResult, DeliveryResult)

    def test_relay_result_is_a_delivery_result(self):
        """The relay subclass satisfies the declared return type."""
        assert issubclass(RelayDeliveryResult, DeliveryResult)


class TestNativeDelivery:
    """The native adapter runs fan-out then confirmation collection."""

    @pytest.mark.asyncio
    async def test_returns_delivered_with_certificate(self, monkeypatch):
        """A successful fan-out and quorum yields DELIVERED."""
        certificate = _certificate()

        async def _fanout(**kwargs):
            return None

        async def _confirm(**kwargs):
            return certificate

        monkeypatch.setattr(delivery_module, "upload_slivers", _fanout)
        monkeypatch.setattr(delivery_module, "collect_confirmations", _confirm)

        result = await NativeDelivery().deliver(
            client=object(),
            committee=object(),
            encoded=_encoded(),
            data=b"raw",
            registration=_registration(),
        )

        assert result.outcome is DeliveryOutcome.DELIVERED
        assert result.certificate is certificate
        assert isinstance(result, NativeDeliveryResult)

    @pytest.mark.asyncio
    async def test_passes_configuration_to_confirmations(self, monkeypatch):
        """Constructor configuration reaches collect_confirmations."""
        seen = {}

        async def _fanout(**kwargs):
            return None

        async def _confirm(**kwargs):
            seen.update(kwargs)
            return _certificate()

        monkeypatch.setattr(delivery_module, "upload_slivers", _fanout)
        monkeypatch.setattr(delivery_module, "collect_confirmations", _confirm)

        encoded = _encoded()
        registration = _registration()

        await NativeDelivery(
            wait_millis=9000,
            max_confirmation_requests=7,
        ).deliver(
            client=object(),
            committee=object(),
            encoded=encoded,
            data=b"raw",
            registration=registration,
        )

        assert seen["wait_millis"] == 9000
        assert seen["max_confirmation_requests"] == 7
        assert seen["blob_id"] == encoded.blob_id
        assert seen["registration"] is registration

    @pytest.mark.asyncio
    async def test_fanout_runs_before_confirmations(self, monkeypatch):
        """Slivers must be uploaded before nodes are polled."""
        order = []

        async def _fanout(**kwargs):
            order.append("fanout")

        async def _confirm(**kwargs):
            order.append("confirm")
            return _certificate()

        monkeypatch.setattr(delivery_module, "upload_slivers", _fanout)
        monkeypatch.setattr(delivery_module, "collect_confirmations", _confirm)

        await NativeDelivery().deliver(
            client=object(),
            committee=object(),
            encoded=_encoded(),
            data=b"raw",
            registration=_registration(),
        )

        assert order == ["fanout", "confirm"]

    @pytest.mark.asyncio
    async def test_reports_both_stage_durations(self, monkeypatch):
        """The two native stages are timed separately."""

        async def _fanout(**kwargs):
            return None

        async def _confirm(**kwargs):
            return _certificate()

        monkeypatch.setattr(delivery_module, "upload_slivers", _fanout)
        monkeypatch.setattr(delivery_module, "collect_confirmations", _confirm)

        result = await NativeDelivery().deliver(
            client=object(),
            committee=object(),
            encoded=_encoded(),
            data=b"raw",
            registration=_registration(),
        )

        assert result.sliver_upload_duration >= 0.0
        assert result.confirmations_duration >= 0.0
        assert result.duration >= 0.0

    @pytest.mark.asyncio
    async def test_fanout_failure_is_reported_with_its_duration(self, monkeypatch):
        """A quorum shortfall is reported, and its duration survives."""
        failure = SliverUploadError(message="no quorum", stage="upload_slivers")

        async def _fanout(**kwargs):
            raise failure

        async def _confirm(**kwargs):
            raise AssertionError("must not be reached")

        monkeypatch.setattr(delivery_module, "upload_slivers", _fanout)
        monkeypatch.setattr(delivery_module, "collect_confirmations", _confirm)

        result = await NativeDelivery().deliver(
            client=object(),
            committee=object(),
            encoded=_encoded(),
            data=b"raw",
            registration=_registration(),
        )

        assert result.outcome is DeliveryOutcome.RESUMABLE
        assert result.certificate is None
        assert result.error is failure
        assert result.sliver_upload_duration >= 0.0

    @pytest.mark.asyncio
    async def test_fanout_failure_leaves_confirmations_duration_none(
        self, monkeypatch
    ):
        """A stage that never ran reports None, never 0.0."""

        async def _fanout(**kwargs):
            raise SliverUploadError(message="no quorum", stage="upload_slivers")

        async def _confirm(**kwargs):
            raise AssertionError("must not be reached")

        monkeypatch.setattr(delivery_module, "upload_slivers", _fanout)
        monkeypatch.setattr(delivery_module, "collect_confirmations", _confirm)

        result = await NativeDelivery().deliver(
            client=object(),
            committee=object(),
            encoded=_encoded(),
            data=b"raw",
            registration=_registration(),
        )

        assert result.confirmations_duration is None

    @pytest.mark.asyncio
    async def test_confirmation_failure_is_reported_with_both_durations(
        self, monkeypatch
    ):
        """A confirmation failure still reports the fan-out's duration."""
        failure = ConfirmationCollectionError(
            message="no quorum", stage="collect_confirmations"
        )

        async def _fanout(**kwargs):
            return None

        async def _confirm(**kwargs):
            raise failure

        monkeypatch.setattr(delivery_module, "upload_slivers", _fanout)
        monkeypatch.setattr(delivery_module, "collect_confirmations", _confirm)

        result = await NativeDelivery().deliver(
            client=object(),
            committee=object(),
            encoded=_encoded(),
            data=b"raw",
            registration=_registration(),
        )

        assert result.outcome is DeliveryOutcome.RESUMABLE
        assert result.error is failure
        assert result.sliver_upload_duration >= 0.0
        assert result.confirmations_duration >= 0.0

    @pytest.mark.asyncio
    async def test_success_reports_no_error(self, monkeypatch):
        """A delivered result carries no error."""

        async def _fanout(**kwargs):
            return None

        async def _confirm(**kwargs):
            return _certificate()

        monkeypatch.setattr(delivery_module, "upload_slivers", _fanout)
        monkeypatch.setattr(delivery_module, "collect_confirmations", _confirm)

        result = await NativeDelivery().deliver(
            client=object(),
            committee=object(),
            encoded=_encoded(),
            data=b"raw",
            registration=_registration(),
        )

        assert result.error is None
        assert result.confirmations_duration is not None


class TestRelayDelivery:
    """The relay adapter POSTs once and parses what comes back."""

    @pytest.mark.asyncio
    async def test_uploaded_yields_parsed_certificate(self, monkeypatch):
        """An UPLOADED result is parsed into a Certificate."""
        certificate = _certificate()
        upload = _upload_result(
            outcome=RelayUploadOutcome.UPLOADED,
            certificate={"signers": [0]},
        )

        async def _post(**kwargs):
            return upload

        def _parse(**kwargs):
            return certificate

        monkeypatch.setattr(delivery_module, "upload_to_relay", _post)
        monkeypatch.setattr(delivery_module, "parse_relay_certificate", _parse)

        result = await RelayDelivery(relay_url="https://relay.example").deliver(
            client=object(),
            committee=object(),
            encoded=_encoded(),
            data=b"raw",
            registration=_registration(),
        )

        assert result.outcome is DeliveryOutcome.DELIVERED
        assert result.certificate is certificate
        assert result.upload is upload

    @pytest.mark.asyncio
    async def test_refused_reports_refused(self, monkeypatch):
        """A deterministic refusal is REFUSED, not RESUMABLE."""
        upload = _upload_result(outcome=RelayUploadOutcome.REFUSED)

        async def _post(**kwargs):
            return upload

        def _parse(**kwargs):
            raise AssertionError("must not be reached")

        monkeypatch.setattr(delivery_module, "upload_to_relay", _post)
        monkeypatch.setattr(delivery_module, "parse_relay_certificate", _parse)

        result = await RelayDelivery(relay_url="https://relay.example").deliver(
            client=object(),
            committee=object(),
            encoded=_encoded(),
            data=b"raw",
            registration=_registration(),
        )

        assert result.outcome is DeliveryOutcome.REFUSED
        assert result.certificate is None
        assert result.upload is upload

    @pytest.mark.asyncio
    async def test_unanswered_reports_resumable(self, monkeypatch):
        """An exhausted attempt budget is resumable, never an error."""
        upload = _upload_result(outcome=RelayUploadOutcome.UNANSWERED)

        async def _post(**kwargs):
            return upload

        monkeypatch.setattr(delivery_module, "upload_to_relay", _post)

        result = await RelayDelivery(relay_url="https://relay.example").deliver(
            client=object(),
            committee=object(),
            encoded=_encoded(),
            data=b"raw",
            registration=_registration(),
        )

        assert result.outcome is DeliveryOutcome.RESUMABLE
        assert result.certificate is None

    @pytest.mark.asyncio
    async def test_uploaded_without_certificate_is_resumable(self, monkeypatch):
        """UPLOADED with no certificate cannot certify, so it is resumable."""
        upload = _upload_result(
            outcome=RelayUploadOutcome.UPLOADED,
            certificate=None,
        )

        async def _post(**kwargs):
            return upload

        monkeypatch.setattr(delivery_module, "upload_to_relay", _post)

        result = await RelayDelivery(relay_url="https://relay.example").deliver(
            client=object(),
            committee=object(),
            encoded=_encoded(),
            data=b"raw",
            registration=_registration(),
        )

        assert result.outcome is DeliveryOutcome.RESUMABLE

    @pytest.mark.asyncio
    async def test_tip_digest_sent_only_when_tip_paid(self, monkeypatch):
        """No tip means no tx_id is sent."""
        seen = {}

        async def _post(**kwargs):
            seen.update(kwargs)
            return _upload_result(outcome=RelayUploadOutcome.UNANSWERED)

        monkeypatch.setattr(delivery_module, "upload_to_relay", _post)

        await RelayDelivery(relay_url="https://relay.example", tip_paid=False).deliver(
            client=object(),
            committee=object(),
            encoded=_encoded(),
            data=b"raw",
            registration=_registration(),
        )
        assert seen["register_tip_tx_digest"] is None

        await RelayDelivery(relay_url="https://relay.example", tip_paid=True).deliver(
            client=object(),
            committee=object(),
            encoded=_encoded(),
            data=b"raw",
            registration=_registration(),
        )
        assert seen["register_tip_tx_digest"] == "TX1DIGEST"

    @pytest.mark.asyncio
    async def test_deletable_object_derived_from_registration(self, monkeypatch):
        """The deletable object ID comes from what was actually registered."""
        seen = {}

        async def _post(**kwargs):
            seen.update(kwargs)
            return _upload_result(outcome=RelayUploadOutcome.UNANSWERED)

        monkeypatch.setattr(delivery_module, "upload_to_relay", _post)

        await RelayDelivery(relay_url="https://relay.example").deliver(
            client=object(),
            committee=object(),
            encoded=_encoded(),
            data=b"raw",
            registration=_registration(deletable=False),
        )
        assert seen["deletable_blob_object"] is None

        registration = _registration(deletable=True)
        await RelayDelivery(relay_url="https://relay.example").deliver(
            client=object(),
            committee=object(),
            encoded=_encoded(),
            data=b"raw",
            registration=registration,
        )
        assert seen["deletable_blob_object"] == registration.object_id

    @pytest.mark.asyncio
    async def test_posts_raw_bytes_not_slivers(self, monkeypatch):
        """The relay is handed the unencoded blob."""
        seen = {}

        async def _post(**kwargs):
            seen.update(kwargs)
            return _upload_result(outcome=RelayUploadOutcome.UNANSWERED)

        monkeypatch.setattr(delivery_module, "upload_to_relay", _post)

        encoded = _encoded()
        await RelayDelivery(
            relay_url="https://relay.example",
            nonce="NONCE",
            max_attempts=3,
        ).deliver(
            client=object(),
            committee=object(),
            encoded=encoded,
            data=b"raw-bytes",
            registration=_registration(),
        )

        assert seen["data"] == b"raw-bytes"
        assert seen["blob_id"] == encoded.blob_id_base64
        assert seen["relay_url"] == "https://relay.example"
        assert seen["nonce"] == "NONCE"
        assert seen["max_attempts"] == 3


class TestDeliveryFailedStage:
    """Every reported failure names the stage it failed at."""

    @pytest.mark.asyncio
    async def test_native_reports_the_failing_stage(self, monkeypatch):
        """A native failure carries the underlying error's stage name."""

        async def _fanout(**kwargs):
            raise SliverUploadError(message="no quorum", stage="upload_slivers")

        async def _confirm(**kwargs):
            raise AssertionError("must not be reached")

        monkeypatch.setattr(delivery_module, "upload_slivers", _fanout)
        monkeypatch.setattr(delivery_module, "collect_confirmations", _confirm)

        result = await NativeDelivery().deliver(
            client=object(),
            committee=object(),
            encoded=_encoded(),
            data=b"raw",
            registration=_registration(),
        )

        assert result.failed_stage == "upload_slivers"

    @pytest.mark.asyncio
    async def test_relay_reports_relay_upload_on_a_refusal(self, monkeypatch):
        """A refused POST is attributed to the upload stage."""

        async def _post(**kwargs):
            return _upload_result(outcome=RelayUploadOutcome.REFUSED)

        monkeypatch.setattr(delivery_module, "upload_to_relay", _post)

        result = await RelayDelivery(relay_url="https://relay.example").deliver(
            client=object(),
            committee=object(),
            encoded=_encoded(),
            data=b"raw",
            registration=_registration(),
        )

        assert result.failed_stage == "relay_upload"

    @pytest.mark.asyncio
    async def test_success_names_no_stage(self, monkeypatch):
        """A delivered result has no failed stage."""

        async def _fanout(**kwargs):
            return None

        async def _confirm(**kwargs):
            return _certificate()

        monkeypatch.setattr(delivery_module, "upload_slivers", _fanout)
        monkeypatch.setattr(delivery_module, "collect_confirmations", _confirm)

        result = await NativeDelivery().deliver(
            client=object(),
            committee=object(),
            encoded=_encoded(),
            data=b"raw",
            registration=_registration(),
        )

        assert result.failed_stage is None


class TestRelayCertificateParseFailure:
    """A relay that answers with an unusable certificate is post-spend."""

    @pytest.mark.asyncio
    async def test_parse_failure_is_reported_not_raised(self, monkeypatch):
        """The tip is already spent, so the failure must be return data."""
        upload = _upload_result(
            outcome=RelayUploadOutcome.UPLOADED,
            certificate={"signers": "not-an-array"},
        )
        failure = RelayCertificateParseError(
            message="signers is not an array", stage="parse_relay_certificate"
        )

        async def _post(**kwargs):
            return upload

        def _parse(**kwargs):
            raise failure

        monkeypatch.setattr(delivery_module, "upload_to_relay", _post)
        monkeypatch.setattr(delivery_module, "parse_relay_certificate", _parse)

        result = await RelayDelivery(relay_url="https://relay.example").deliver(
            client=object(),
            committee=object(),
            encoded=_encoded(),
            data=b"raw",
            registration=_registration(),
        )

        assert result.outcome is DeliveryOutcome.RESUMABLE
        assert result.certificate is None
        assert result.error is failure

    @pytest.mark.asyncio
    async def test_parse_failure_is_attributed_to_certify_not_upload(
        self, monkeypatch
    ):
        """The POST succeeded, so blaming the upload stage would mislead."""

        async def _post(**kwargs):
            return _upload_result(
                outcome=RelayUploadOutcome.UPLOADED,
                certificate={"signers": "not-an-array"},
            )

        def _parse(**kwargs):
            raise RelayCertificateParseError(
                message="signers is not an array", stage="parse_relay_certificate"
            )

        monkeypatch.setattr(delivery_module, "upload_to_relay", _post)
        monkeypatch.setattr(delivery_module, "parse_relay_certificate", _parse)

        result = await RelayDelivery(relay_url="https://relay.example").deliver(
            client=object(),
            committee=object(),
            encoded=_encoded(),
            data=b"raw",
            registration=_registration(),
        )

        assert result.failed_stage == "certify"

    @pytest.mark.asyncio
    async def test_resumption_tokens_survive_a_parse_failure(self, monkeypatch):
        """The POST's report is retained so a later attempt can resume."""
        upload = _upload_result(
            outcome=RelayUploadOutcome.UPLOADED,
            certificate={"signers": "not-an-array"},
        )

        async def _post(**kwargs):
            return upload

        def _parse(**kwargs):
            raise RelayCertificateParseError(
                message="signers is not an array", stage="parse_relay_certificate"
            )

        monkeypatch.setattr(delivery_module, "upload_to_relay", _post)
        monkeypatch.setattr(delivery_module, "parse_relay_certificate", _parse)

        result = await RelayDelivery(relay_url="https://relay.example").deliver(
            client=object(),
            committee=object(),
            encoded=_encoded(),
            data=b"raw",
            registration=_registration(),
        )

        assert result.upload is upload

    @pytest.mark.asyncio
    async def test_relay_success_carries_no_error(self, monkeypatch):
        """A parsed certificate leaves error unset."""

        async def _post(**kwargs):
            return _upload_result(
                outcome=RelayUploadOutcome.UPLOADED,
                certificate={"signers": [0]},
            )

        def _parse(**kwargs):
            return _certificate()

        monkeypatch.setattr(delivery_module, "upload_to_relay", _post)
        monkeypatch.setattr(delivery_module, "parse_relay_certificate", _parse)

        result = await RelayDelivery(relay_url="https://relay.example").deliver(
            client=object(),
            committee=object(),
            encoded=_encoded(),
            data=b"raw",
            registration=_registration(),
        )

        assert result.error is None
        assert result.failed_stage is None
