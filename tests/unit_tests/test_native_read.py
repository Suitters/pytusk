#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for the native read pipeline's error family and result type."""

import dataclasses

import pytest
from pysui import SuiRpcResult

import pytusk
from pytusk.commands.node_commands import (
    GetMetadata,
    GetSliver,
    MetadataData,
    SliverData,
)
from pytusk.core.chain import WalrusCommittee, WalrusCommitteeMember
from pytusk.core.encoding import (
    BlobDecodeError,
    SliverVerificationError,
    encode_blob,
    shard_index_to_pair_index,
    source_symbol_counts,
    verify_blob_metadata,
)
from pytusk.core.native_read import fetch_slivers, fetch_verified_metadata
from pytusk.core.native_read.reconstruct import _sliver_cap
from pytusk.core.pipelines.read import read_blob_native
from pytusk.core.types.errors import (
    MetadataFetchError,
    NativeReadError,
    NativeUploadError,
    SliverFetchError,
)
from pytusk.core.types.protocols import UploadReceipt
from pytusk.core.types.receipts import NativeReadResult

_N_SHARDS = 10
_ROTATION_BLOB_ID = b"\x00" * 31 + b"\x05"


def _committee(*, n_shards: int = _N_SHARDS) -> WalrusCommittee:
    """Build a committee with exactly one shard per node."""
    members = tuple(
        WalrusCommitteeMember(
            node_id=f"node{index}",
            shard_indices=(index,),
            network_address=f"node{index}.example:443",
            public_key=b"\x00" * 48,
        )
        for index in range(n_shards)
    )
    return WalrusCommittee(epoch=7, n_shards=n_shards, members=members)


class _FakeMetadataClient:
    """Serves canned metadata bytes per node base URL."""

    def __init__(self, *, payloads: dict[str, bytes | None]) -> None:
        """Store the per-node payloads; None means that node fails."""
        self.payloads = payloads
        self.calls: list[str] = []

    async def execute(
        self,
        *,
        command: object,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
        base_url: str | None = None,
    ) -> SuiRpcResult:
        """Return the canned metadata for this node."""
        assert base_url is not None
        self.calls.append(base_url)
        if isinstance(command, GetMetadata):
            payload = self.payloads.get(base_url)
            if payload is None:
                return SuiRpcResult(False, "metadata unavailable")
            return SuiRpcResult(True, "", MetadataData(content=payload))
        raise NotImplementedError(f"Unhandled command: {type(command)}")


class _FakeSliverClient:
    """Serves canned sliver bytes keyed by sliver-pair index."""

    def __init__(
        self,
        *,
        slivers_by_pair: dict[int, bytes],
        failing_nodes: frozenset[str] = frozenset(),
    ) -> None:
        """Store the canned slivers and the set of failing base URLs."""
        self.slivers_by_pair = slivers_by_pair
        self.failing_nodes = failing_nodes
        self.requests: list[tuple[str, int, str]] = []

    async def execute(
        self,
        *,
        command: object,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
        base_url: str | None = None,
    ) -> SuiRpcResult:
        """Return the canned sliver for the requested pair index."""
        assert base_url is not None
        if isinstance(command, GetSliver):
            self.requests.append(
                (base_url, command.sliver_pair_index, command.sliver_type)
            )
            if base_url in self.failing_nodes:
                return SuiRpcResult(False, "node down")
            data = self.slivers_by_pair.get(command.sliver_pair_index)
            if data is None:
                return SuiRpcResult(False, "sliver missing")
            return SuiRpcResult(True, "", SliverData(content=data))
        raise NotImplementedError(f"Unhandled command: {type(command)}")


class TestNativeReadErrorFamily:
    """The read pipeline's error hierarchy."""

    @pytest.mark.parametrize(
        "error_type",
        [MetadataFetchError, SliverFetchError],
    )
    def test_subclasses_share_the_read_base(
        self, error_type: type[NativeReadError]
    ) -> None:
        """Every read error is catchable as NativeReadError."""
        error = error_type(message="boom", stage="fetch_slivers")
        assert isinstance(error, NativeReadError)
        assert isinstance(error, RuntimeError)

    def test_stage_is_recorded(self) -> None:
        """The failing stage is carried on the exception."""
        error = SliverFetchError(message="too few slivers", stage="fetch_slivers")
        assert error.stage == "fetch_slivers"
        assert str(error) == "too few slivers"

    def test_upload_handler_does_not_catch_read_errors(self) -> None:
        """REGRESSION: the read and upload families are separate hierarchies.

        An ``except NativeUploadError`` guarding the write path must not
        swallow a read failure. This is why NativeReadError deliberately does
        not share a base with NativeUploadError.
        """
        assert not issubclass(NativeReadError, NativeUploadError)
        assert not issubclass(NativeUploadError, NativeReadError)
        with pytest.raises(SliverFetchError):
            try:
                raise SliverFetchError(message="boom", stage="fetch_slivers")
            except NativeUploadError:  # pragma: no cover - must not trigger
                pytest.fail("NativeUploadError caught a read-path error")

    def test_carries_no_duration_field(self) -> None:
        """The read family deliberately has no always-None duration field."""
        error = SliverFetchError(message="boom", stage="fetch_slivers")
        assert not hasattr(error, "duration")


class TestNativeReadResult:
    """The native read pipeline's result type."""

    @staticmethod
    def _result() -> NativeReadResult:
        """Build a representative result for assertions."""
        return NativeReadResult(
            content=b"reconstructed",
            blob_id=b"\x01" * 32,
            epoch=42,
            axis="primary",
            slivers_used=334,
        )

    def test_holds_its_fields(self) -> None:
        """All fields round-trip through the constructor."""
        result = self._result()
        assert result.content == b"reconstructed"
        assert result.blob_id == b"\x01" * 32
        assert result.epoch == 42
        assert result.axis == "primary"
        assert result.slivers_used == 334

    def test_is_frozen(self) -> None:
        """The result is immutable."""
        result = self._result()
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.epoch = 43  # type: ignore[misc]

    def test_requires_keyword_arguments(self) -> None:
        """Fields are keyword-only."""
        with pytest.raises(TypeError):
            NativeReadResult(b"x", b"\x01" * 32, 1, "primary", 1)  # type: ignore[call-arg]

    def test_does_not_satisfy_upload_receipt(self) -> None:
        """REGRESSION: a read result is NOT an UploadReceipt.

        UploadReceipt requires certified / register_tx_digest /
        certify_tx_digest / timings -- all write-path concepts that would be
        meaningless Optionals on a read. This asserts the two stay separate
        rather than being "unified" later.
        """
        assert not isinstance(self._result(), UploadReceipt)


class TestFetchVerifiedMetadata:
    """The metadata stage of the native read pipeline."""

    @staticmethod
    def _outer_metadata(data: bytes, n_shards: int) -> tuple[bytes, bytes]:
        """Encode data and return its (blob_id, outer metadata bytes)."""
        encoded = encode_blob(data=data, n_shards=n_shards)
        return encoded.blob_id, encoded.blob_id + encoded.metadata_bcs

    async def test_returns_verified_metadata(self) -> None:
        """Metadata served by the committee is verified and returned."""
        committee = _committee()
        data = b"a blob whose metadata every node serves correctly"
        blob_id, outer = self._outer_metadata(data, committee.n_shards)
        client = _FakeMetadataClient(
            payloads={member.base_url: outer for member in committee.members}
        )

        metadata = await fetch_verified_metadata(
            client=client, committee=committee, blob_id=blob_id
        )

        assert metadata.blob_id == blob_id
        assert metadata.unencoded_length == len(data)
        assert metadata.n_shards == committee.n_shards

    async def test_metadata_for_a_different_blob_is_rejected(self) -> None:
        """REGRESSION: internally-valid metadata for the WRONG blob is refused.

        Verification alone only proves self-consistency. A node returning
        perfectly valid metadata for some other blob must not satisfy a read
        of this one -- the chain-sourced blob ID comparison is what binds the
        two together.
        """
        committee = _committee()
        wanted_blob_id, _ = self._outer_metadata(b"the blob asked for", 10)
        _, other_outer = self._outer_metadata(b"an entirely different blob", 10)
        client = _FakeMetadataClient(
            payloads={member.base_url: other_outer for member in committee.members}
        )

        with pytest.raises(MetadataFetchError):
            await fetch_verified_metadata(
                client=client, committee=committee, blob_id=wanted_blob_id
            )

    async def test_all_nodes_failing_raises(self) -> None:
        """No node answering yields MetadataFetchError."""
        committee = _committee()
        blob_id, _ = self._outer_metadata(b"nobody serves this", 10)
        client = _FakeMetadataClient(
            payloads={member.base_url: None for member in committee.members}
        )

        with pytest.raises(MetadataFetchError):
            await fetch_verified_metadata(
                client=client, committee=committee, blob_id=blob_id
            )

    async def test_unverifiable_bytes_are_skipped(self) -> None:
        """Garbage metadata is skipped rather than propagating a decode error."""
        committee = _committee()
        blob_id, _ = self._outer_metadata(b"garbage everywhere", 10)
        client = _FakeMetadataClient(
            payloads={member.base_url: b"\x00" * 64 for member in committee.members}
        )

        with pytest.raises(MetadataFetchError):
            await fetch_verified_metadata(
                client=client, committee=committee, blob_id=blob_id
            )

    async def test_empty_committee_raises(self) -> None:
        """A committee with no members cannot serve metadata."""
        committee = WalrusCommittee(epoch=1, n_shards=10, members=())
        client = _FakeMetadataClient(payloads={})

        with pytest.raises(MetadataFetchError):
            await fetch_verified_metadata(
                client=client, committee=committee, blob_id=b"\x01" * 32
            )


class TestFetchSlivers:
    """The sliver fan-out stage of the native read pipeline."""

    @staticmethod
    def _all_pairs() -> dict[int, bytes]:
        """Canned sliver bytes for every pair index."""
        return {index: bytes([index]) * 8 for index in range(_N_SHARDS)}

    async def test_collects_at_least_the_threshold(self) -> None:
        """A healthy committee yields at least the requested threshold."""
        committee = _committee()
        threshold, _ = source_symbol_counts(n_shards=committee.n_shards)
        client = _FakeSliverClient(slivers_by_pair=self._all_pairs())

        slivers = await fetch_slivers(
            client=client,
            committee=committee,
            blob_id=_ROTATION_BLOB_ID,
            axis="primary",
            threshold=threshold,
        )

        assert len(slivers) >= threshold

    async def test_requests_rotated_pair_indices_not_shard_indices(self) -> None:
        """REGRESSION: the URL takes a SLIVER-PAIR index, not a shard index.

        The two differ by a per-blob rotation. Sending the raw shard index
        would fetch the wrong sliver from every node, and because the set of
        all pair indices equals the set of all shard indices, only a
        per-node check can catch it.
        """
        committee = _committee()
        threshold, _ = source_symbol_counts(n_shards=committee.n_shards)
        client = _FakeSliverClient(slivers_by_pair=self._all_pairs())

        await fetch_slivers(
            client=client,
            committee=committee,
            blob_id=_ROTATION_BLOB_ID,
            axis="primary",
            threshold=threshold,
        )

        requested_by_node = {
            base_url: pair_index for base_url, pair_index, _ in client.requests
        }
        differed = 0
        for member in committee.members:
            if member.base_url not in requested_by_node:
                continue
            shard_index = member.shard_indices[0]
            expected = shard_index_to_pair_index(
                shard_index=shard_index,
                blob_id=_ROTATION_BLOB_ID,
                n_shards=committee.n_shards,
            )
            assert requested_by_node[member.base_url] == expected
            if expected != shard_index:
                differed += 1
        assert differed > 0

    async def test_axis_is_passed_through(self) -> None:
        """The requested axis reaches the sliver URL unchanged."""
        committee = _committee()
        _, threshold = source_symbol_counts(n_shards=committee.n_shards)
        client = _FakeSliverClient(slivers_by_pair=self._all_pairs())

        await fetch_slivers(
            client=client,
            committee=committee,
            blob_id=_ROTATION_BLOB_ID,
            axis="secondary",
            threshold=threshold,
        )

        assert {sliver_type for _, _, sliver_type in client.requests} == {"secondary"}

    async def test_below_threshold_raises(self) -> None:
        """Too few responding nodes yields SliverFetchError."""
        committee = _committee()
        threshold, _ = source_symbol_counts(n_shards=committee.n_shards)
        failing = frozenset(member.base_url for member in committee.members[1:])
        client = _FakeSliverClient(
            slivers_by_pair=self._all_pairs(), failing_nodes=failing
        )

        with pytest.raises(SliverFetchError):
            await fetch_slivers(
                client=client,
                committee=committee,
                blob_id=_ROTATION_BLOB_ID,
                axis="primary",
                threshold=threshold,
            )

    async def test_duplicate_slivers_do_not_count_toward_threshold(self) -> None:
        """REGRESSION: the threshold counts DISTINCT slivers, not responses.

        A node holding k shards can answer all k of its requests with one
        valid sliver. Counting raw responses would let that advance the
        threshold by k while contributing a single symbol: the fan-out would
        stop early, cancel the honest stragglers still in flight, and hand
        the decoder fewer distinct symbols than it needs. Every node here
        serves byte-identical content, so the whole committee is worth one
        sliver and the threshold must not be reachable.
        """
        committee = _committee()
        threshold, _ = source_symbol_counts(n_shards=committee.n_shards)
        duplicate = b"\xab" * 8
        client = _FakeSliverClient(
            slivers_by_pair={index: duplicate for index in range(_N_SHARDS)}
        )

        with pytest.raises(SliverFetchError):
            await fetch_slivers(
                client=client,
                committee=committee,
                blob_id=_ROTATION_BLOB_ID,
                axis="primary",
                threshold=threshold,
            )

    async def test_distinct_slivers_survive_deduplication(self) -> None:
        """Deduplication drops only repeats, never distinct slivers."""
        committee = _committee()
        threshold, _ = source_symbol_counts(n_shards=committee.n_shards)
        shared = b"\xab" * 8
        by_pair = {
            index: (shared if index % 2 == 0 else bytes([index]) * 8)
            for index in range(_N_SHARDS)
        }
        client = _FakeSliverClient(slivers_by_pair=by_pair)

        slivers = await fetch_slivers(
            client=client,
            committee=committee,
            blob_id=_ROTATION_BLOB_ID,
            axis="primary",
            threshold=threshold,
        )

        assert len(slivers) == len(set(slivers))
        assert len(slivers) >= threshold


class _FakeReadClient:
    """Serves a committee, canned metadata bytes, and canned slivers."""

    def __init__(
        self,
        *,
        committee: WalrusCommittee,
        metadata: bytes,
        slivers_by_pair: dict[int, bytes],
        committee_error: Exception | None = None,
    ) -> None:
        """Store the canned responses and any committee-read failure."""
        self._committee = committee
        self.metadata = metadata
        self.slivers_by_pair = slivers_by_pair
        self.committee_error = committee_error
        self.executes = 0

    async def committee(self) -> WalrusCommittee:
        """Return the canned committee or raise the canned failure."""
        if self.committee_error is not None:
            raise self.committee_error
        return self._committee

    async def execute(
        self,
        *,
        command: object,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
        base_url: str | None = None,
    ) -> SuiRpcResult:
        """Serve metadata and sliver requests from the canned data."""
        self.executes += 1
        if isinstance(command, GetMetadata):
            return SuiRpcResult(True, "", MetadataData(content=self.metadata))
        if isinstance(command, GetSliver):
            data = self.slivers_by_pair.get(command.sliver_pair_index)
            if data is None:
                return SuiRpcResult(False, "sliver missing")
            return SuiRpcResult(True, "", SliverData(content=data))
        raise NotImplementedError(f"Unhandled command: {type(command)}")


class TestReadBlobNative:
    """The native read pipeline's orchestration.

    These cover what ``read_blob_native`` itself owns -- threshold choice,
    axis routing, epoch reporting, sliver accounting and error translation.
    ``decode_blob`` is stubbed: the RedStuff round trip belongs where the
    codec lives, and stubbing it here lets each test assert exactly what the
    pipeline handed the decoder.
    """

    _DATA = b"a blob read back from its own slivers"

    @staticmethod
    def _stub_decode(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
        """Replace decode_blob with a recorder returning a sentinel."""
        captured: dict[str, object] = {}

        def _fake_decode(
            *,
            slivers: object,
            metadata: object,
            axis: str,
            verify: bool,
        ) -> bytes:
            captured["slivers"] = list(slivers)  # type: ignore[call-overload]
            captured["axis"] = axis
            captured["verify"] = verify
            return b"DECODED"

        monkeypatch.setattr(
            "pytusk.core.native_read.reconstruct.decode_blob", _fake_decode
        )
        return captured

    @classmethod
    def _client(
        cls,
        *,
        served_pairs: int = _N_SHARDS,
        committee_error: Exception | None = None,
    ) -> tuple[_FakeReadClient, bytes]:
        """Build a client serving real metadata plus `served_pairs` slivers."""
        committee = _committee()
        encoded = encode_blob(data=cls._DATA, n_shards=committee.n_shards)
        client = _FakeReadClient(
            committee=committee,
            metadata=encoded.blob_id + encoded.metadata_bcs,
            slivers_by_pair={
                index: bytes([index]) * 8 for index in range(served_pairs)
            },
            committee_error=committee_error,
        )
        return client, encoded.blob_id

    async def test_returns_the_decoded_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The result carries whatever the decoder produced."""
        self._stub_decode(monkeypatch)
        client, blob_id = self._client()

        result = await read_blob_native(client=client, blob_id=blob_id)

        assert result.content == b"DECODED"
        assert result.blob_id == blob_id

    async def test_reports_the_committee_epoch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The epoch comes from the committee the read actually used."""
        self._stub_decode(monkeypatch)
        client, blob_id = self._client()

        result = await read_blob_native(client=client, blob_id=blob_id)

        assert result.epoch == 7

    async def test_primary_axis_reaches_the_decoder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The requested axis is routed through to the decode call."""
        captured = self._stub_decode(monkeypatch)
        client, blob_id = self._client()

        result = await read_blob_native(client=client, blob_id=blob_id)

        assert captured["axis"] == "primary"
        assert result.axis == "primary"

    async def test_primary_succeeds_on_the_primary_threshold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exactly the primary threshold of slivers is enough for primary."""
        self._stub_decode(monkeypatch)
        primary, _ = source_symbol_counts(n_shards=_N_SHARDS)
        client, blob_id = self._client(served_pairs=primary)

        result = await read_blob_native(
            client=client, blob_id=blob_id, axis="primary"
        )

        assert result.slivers_used >= primary

    async def test_secondary_uses_the_secondary_threshold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REGRESSION: secondary must take the SECOND tuple element.

        ``source_symbol_counts`` returns ``(primary, secondary)``. Here there
        are exactly enough slivers for primary and not enough for secondary,
        so reading the wrong element lets a secondary read proceed on too few
        slivers -- which then fails inside the decoder as an unexplained
        decode error rather than as the shortfall it actually is.
        """
        self._stub_decode(monkeypatch)
        primary, secondary = source_symbol_counts(n_shards=_N_SHARDS)
        assert primary < secondary, "premise: the two thresholds must differ"
        client, blob_id = self._client(served_pairs=primary)

        with pytest.raises(SliverFetchError):
            await read_blob_native(
                client=client, blob_id=blob_id, axis="secondary"
            )

    async def test_verify_flag_reaches_the_decoder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """verify=False is forwarded rather than silently defaulted."""
        captured = self._stub_decode(monkeypatch)
        client, blob_id = self._client()

        await read_blob_native(client=client, blob_id=blob_id, verify=False)

        assert captured["verify"] is False

    async def test_slivers_used_counts_what_was_decoded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """slivers_used is the count fed to the decoder, not the threshold."""
        captured = self._stub_decode(monkeypatch)
        client, blob_id = self._client()

        result = await read_blob_native(client=client, blob_id=blob_id)

        assert result.slivers_used == len(captured["slivers"])  # type: ignore[arg-type]

    async def test_committee_failure_becomes_a_native_read_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed chain read is translated, and names its stage."""
        self._stub_decode(monkeypatch)
        client, blob_id = self._client(committee_error=RuntimeError("no node"))

        with pytest.raises(NativeReadError) as caught:
            await read_blob_native(client=client, blob_id=blob_id)

        assert caught.value.stage == "committee"
        assert not isinstance(caught.value, (MetadataFetchError, SliverFetchError))

    async def test_rejects_a_wrong_length_blob_id_before_any_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A caller error is caught before the pipeline touches the network."""
        self._stub_decode(monkeypatch)
        client, _ = self._client()

        with pytest.raises(ValueError):
            await read_blob_native(client=client, blob_id=b"\x01" * 31)

        assert client.executes == 0


class TestReconstructBlob:
    """Recovery when a node serves slivers the decoder cannot use.

    ``decode_blob`` and ``verify_sliver`` are stubbed for the same reason
    the pipeline tests stub the decoder: what this stage owns is the
    indict-refetch-redecode ORDERING, and driving it with real RedStuff
    would exercise the codec instead of the orchestration.
    """

    @staticmethod
    def _client(*, served_pairs: int = _N_SHARDS) -> tuple[_FakeReadClient, bytes]:
        """Build a client serving real metadata plus canned slivers."""
        committee = _committee()
        encoded = encode_blob(data=b"x" * 64, n_shards=committee.n_shards)
        client = _FakeReadClient(
            committee=committee,
            metadata=encoded.blob_id + encoded.metadata_bcs,
            slivers_by_pair={
                index: bytes([index]) * 8 for index in range(served_pairs)
            },
        )
        return client, encoded.blob_id

    @staticmethod
    def _stub(
        monkeypatch: pytest.MonkeyPatch,
        *,
        decode_failures: int,
        bad_slivers: frozenset[bytes],
    ) -> dict[str, object]:
        """Stub the decoder and the sliver verifier.

        The first ``decode_failures`` decode calls raise before one is
        allowed to succeed; ``bad_slivers`` are the payloads verification
        rejects, standing in for what a dishonest node served.
        """
        calls: dict[str, object] = {"decodes": 0, "verifies": 0}

        def _fake_decode(
            *,
            slivers: object,
            metadata: object,
            axis: str,
            verify: bool,
        ) -> bytes:
            calls["decodes"] = int(calls["decodes"]) + 1  # type: ignore[call-overload]
            if int(calls["decodes"]) <= decode_failures:  # type: ignore[call-overload]
                raise BlobDecodeError("RedStuff decode failed (invalid_sliver_bcs)")
            calls["slivers"] = list(slivers)  # type: ignore[call-overload]
            return b"RECOVERED"

        def _fake_verify(*, sliver: bytes, metadata: object, axis: str) -> None:
            calls["verifies"] = int(calls["verifies"]) + 1  # type: ignore[call-overload]
            if sliver in bad_slivers:
                raise SliverVerificationError(
                    "Sliver failed verification (merkle_root_mismatch)"
                )

        monkeypatch.setattr(
            "pytusk.core.native_read.reconstruct.decode_blob", _fake_decode
        )
        monkeypatch.setattr(
            "pytusk.core.native_read.reconstruct.verify_sliver", _fake_verify
        )
        return calls

    async def test_a_bad_sliver_is_dropped_and_the_read_still_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REGRESSION: one bad sliver must not deny the whole read.

        The decoder is all-or-nothing at its BCS boundary, so a single
        unparseable sliver discards every good sliver gathered alongside it.
        Once the culprit is identified the survivors still clear the
        threshold, so they are decoded directly without a second fan-out.
        """
        client, blob_id = self._client()
        bad = bytes([0]) * 8
        calls = self._stub(
            monkeypatch, decode_failures=1, bad_slivers=frozenset({bad})
        )

        result = await read_blob_native(client=client, blob_id=blob_id)

        assert result.content == b"RECOVERED"
        assert calls["decodes"] == 2
        assert bad not in calls["slivers"]  # type: ignore[operator]

    async def test_unattributable_decode_failure_is_not_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure no sliver can be blamed for is raised as it stands.

        If every sliver verifies, no node can be indicted and a second
        fan-out would fetch the same bytes to the same end, so the original
        decode failure is the honest answer.
        """
        client, blob_id = self._client()
        calls = self._stub(
            monkeypatch, decode_failures=99, bad_slivers=frozenset()
        )

        with pytest.raises(BlobDecodeError):
            await read_blob_native(client=client, blob_id=blob_id)

        assert calls["decodes"] == 1

    async def test_too_few_survivors_triggers_a_second_fan_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When survivors fall short, the fan-out reruns without the culprits.

        Barring most of the committee leaves the honest remainder unable to
        reach the threshold, and that shortfall is reported as one -- which
        is only reachable if the second fan-out actually ran with the
        indicted nodes excluded.
        """
        client, blob_id = self._client()
        bad = frozenset(bytes([index]) * 8 for index in range(8))
        self._stub(monkeypatch, decode_failures=1, bad_slivers=bad)

        with pytest.raises(SliverFetchError):
            await read_blob_native(client=client, blob_id=blob_id)


class TestResponseCaps:
    """Response-size caps on the read path's storage-node requests.

    A node answering a ~64 KB metadata request with a gigabyte exhausts the
    client before anything has validated a byte. These pin the plumbing: a
    cap set on a command is what the client will enforce, and the computed
    sliver bound is never smaller than a sliver the encoder really produced.
    """

    def test_a_command_reports_the_cap_it_was_given(self) -> None:
        """The cap a caller sets is what the client reads back off it."""
        capped = GetSliver(
            blob_id=_ROTATION_BLOB_ID,
            sliver_pair_index=0,
            sliver_type="primary",
            max_bytes=4096,
        )

        assert capped.max_response_bytes() == 4096

    def test_a_command_is_uncapped_by_default(self) -> None:
        """Omitting the cap leaves the response unbounded.

        The default has to be None rather than some number: an aggregator
        read returns a whole blob, and no fixed ceiling is correct for it.
        """
        assert GetMetadata(blob_id=_ROTATION_BLOB_ID).max_response_bytes() is None

    def test_the_sliver_cap_exceeds_a_real_sliver(self) -> None:
        """REGRESSION: a cap under a genuine sliver would reject honest nodes.

        The bound is computed from the metadata rather than measured, so it
        is checked here against slivers the encoder actually produced. A cap
        that can produce false rejections is worse than a slightly loose one.
        """
        committee = _committee()
        encoded = encode_blob(data=b"y" * 512, n_shards=committee.n_shards)
        metadata = verify_blob_metadata(
            metadata_bcs=encoded.blob_id + encoded.metadata_bcs,
            n_shards=committee.n_shards,
        )

        cap = _sliver_cap(metadata=metadata)

        assert cap > 0
        assert cap >= len(encoded.slivers[0].primary)
        assert cap >= len(encoded.slivers[0].secondary)


class TestReadPathPublicSurface:
    """The native read path is reachable from top-level ``pytusk``.

    The project rule is that neither the tusky CLI nor SDK users import from
    ``pytusk.core.*``, so every public read-path name has to be exported from
    the top-level package. ``test_public_surface_types`` walks the signatures
    of names ALREADY in ``__all__`` and so cannot notice a function that was
    never exported at all -- which is exactly the gap this closes. Without it
    the read path would only fail at the point the CLI tried to import it.
    """

    _READ_PATH_NAMES = (
        "MetadataFetchError",
        "NativeReadError",
        "NativeReadResult",
        "SliverFetchError",
        "SliverVerificationError",
        "fetch_slivers",
        "fetch_verified_metadata",
        "read_blob_native",
        "reconstruct_blob",
        "verify_sliver",
    )

    @pytest.mark.parametrize("name", _READ_PATH_NAMES)
    def test_name_is_declared_public(self, name: str) -> None:
        """The name is listed in pytusk.__all__."""
        assert name in pytusk.__all__

    @pytest.mark.parametrize("name", _READ_PATH_NAMES)
    def test_name_is_actually_bound(self, name: str) -> None:
        """REGRESSION: listed in __all__ is not the same as importable.

        A name added to ``__all__`` without a matching import is invisible
        until someone runs ``from pytusk import *``, which then raises
        AttributeError naming a symbol the module appears to promise.
        """
        assert hasattr(pytusk, name)
