#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for ``pytusk.core.ops.blob_compose`` and
``pytusk.core.ops.blob_execute``.

Scope, deliberately narrow: this module's real work is building and
submitting PTBs against a live pysui client (``execute_reserve_and_register``,
``execute_certify``, ``resolve_package_id``, ``select_wal_payment_coin``).
None of that is meaningfully unit-testable without either a live node or an
elaborate mock of ``AsyncSuiTransaction``/``WalrusClient`` that would fake
coverage rather than prove behaviour -- so none of it is tested here. What IS
tested: the pure/local helpers that do not require a client at all
(``blob_deletable_and_end_epoch``, ``find_created_object_id``,
``require_success``, the ``_encoded_storage_amount`` blocker) and the
``Registration``/``CertifyResult`` dataclasses (construction, frozen,
keyword-only). See the deliverable-4 report for what is deferred to the live
testnet run and why.

Two narrow exceptions to that boundary: ``add_certify`` and
``add_reserve_and_register`` are PURE PTB COMPOSITION (see the module
docstring's "caller owns the transaction lifecycle" note) -- they make no
network calls, build nothing, sign nothing, and submit nothing, so a
minimal recording fake standing in for ``AsyncSuiTransaction`` (see
``_RecordingTxn`` below) genuinely proves their move_call/transfer_objects
ordering rather than faking coverage. ``TestAddCertifyTransferOrdering``
covers ``add_certify``'s recipient-conditional transfer.
``TestExecuteReserveAndRegisterTransfersToSender`` drives the actual thin
wrapper ``execute_reserve_and_register`` with the same recording fake plus
a stub client that fails ``ExecuteTransaction`` submission immediately --
just far enough to observe the wrapper's own ``transfer_objects`` call
before its read-back machinery (``find_created_object_id``,
``wait_for_finality``, ``GetObject``) would otherwise force the "elaborate
mock" this module deliberately avoids.
"""

import dataclasses
import types
from typing import Any

import pysui.sui.sui_grpc.suimsgs.sui.rpc.v2 as sui_prot
import pytest
from pysui import SuiRpcResult
from pysui.sui.sui_common.txn_transaction_builder import ProgrammableTransactionBuilder

from pytusk.core.certification import Certificate
from pytusk.core.chain import (
    blob_deletable_and_end_epoch,
    find_created_object_id,
    find_created_shared_object_id,
    require_success,
)
from pytusk.core.encoding import EncodedBlob, encoded_blob_length
from pytusk.core.ops import blob_execute, system_reads
from pytusk.core.ops.blob_compose import (
    _encoded_storage_amount,
    add_certify,
    add_registration_sequence,
)
from pytusk.core.ops.blob_execute import (
    execute_reserve_and_register,
    preflight_payment,
    preflight_sponsor,
)
from pytusk.core.ops.system_reads import wait_for_finality
from pytusk.core.relay_upload.tip import build_auth_package
from pytusk.core.types import (
    FROM_GAS,
    CertifyResult,
    Registration,
    RelayUploadError,
    TipComposition,
)

_BLOB_ID = b"\x01" * 32


class _FakeValue:
    """Stand-in for a protobuf ``Value`` where only the set field exists.

    Mirrors the fake used in ``test_committee.py``: an unset oneof member
    reads back as absent entirely (``getattr(..., default)``), matching
    betterproto's behaviour.
    """

    def __init__(self, **fields: object) -> None:
        for name, value in fields.items():
            setattr(self, name, value)


class _FakeStruct:
    """Stand-in for a protobuf ``Struct``."""

    def __init__(self, *, fields: dict[str, object]) -> None:
        self.fields = fields


class _FakeJson:
    """Stand-in for ``Object.json``."""

    def __init__(self, *, struct_value: _FakeStruct | None) -> None:
        self.struct_value = struct_value


class _FakeObject:
    """Stand-in for ``sui_prot.Object``, holding only what the helper reads."""

    def __init__(self, *, object_id: str, json: _FakeJson | None) -> None:
        self.object_id = object_id
        self.json = json


def _blob_object(*, end_epoch: float | None = 42.0, deletable: bool | None = True):
    fields: dict[str, object] = {}
    if end_epoch is not None:
        storage_fields: dict[str, object] = {"end_epoch": _FakeValue(number_value=end_epoch)}
        fields["storage"] = _FakeValue(struct_value=_FakeStruct(fields=storage_fields))
    if deletable is not None:
        fields["deletable"] = _FakeValue(bool_value=deletable)
    return _FakeObject(
        object_id="0xblob", json=_FakeJson(struct_value=_FakeStruct(fields=fields))
    )


class _FakeOwner:
    """Stand-in for ``sui_prot.Owner``."""

    def __init__(
        self, *, address: str | None = None, kind: object | None = None
    ) -> None:
        self.address = address
        self.kind = kind


class _FakeChangedObject:
    """Stand-in for ``sui_prot.ChangedObject``."""

    def __init__(
        self,
        *,
        object_id: str | None,
        id_operation: object,
        output_owner: _FakeOwner | None = None,
        object_type: str | None = None,
    ) -> None:
        self.object_id = object_id
        self.id_operation = id_operation
        self.output_owner = output_owner
        self.object_type = object_type


class _FakeEffects:
    """Stand-in for ``sui_prot.TransactionEffects``."""

    def __init__(
        self,
        *,
        changed_objects: list[_FakeChangedObject] | None = None,
        status: object | None = None,
    ) -> None:
        self.changed_objects = changed_objects or []
        self.status = status


class _FakeExecutionError:
    """Stand-in for ``sui_prot.ExecutionError``."""

    def __init__(self, *, description: str) -> None:
        self.description = description


class _FakeStatus:
    """Stand-in for ``sui_prot.ExecutionStatus``."""

    def __init__(self, *, success: bool, error: _FakeExecutionError | None = None) -> None:
        self.success = success
        self.error = error


class _FakeResultData:
    """Stand-in for the ``result_data`` an ``ExecuteTransaction`` result carries."""

    def __init__(self, *, effects: _FakeEffects | None) -> None:
        self.effects = effects


class TestRegistration:
    """Construction, immutability, and keyword-only enforcement."""

    def _registration(self) -> Registration:
        return Registration(
            object_id="0xblob",
            blob_id=_BLOB_ID,
            end_epoch=42,
            deletable=True,
            digest="0xdigest1",
        )

    def test_construction(self) -> None:
        """All fields round-trip as given."""
        registration = self._registration()
        assert registration.object_id == "0xblob"
        assert registration.blob_id == _BLOB_ID
        assert registration.end_epoch == 42
        assert registration.deletable is True
        assert registration.digest == "0xdigest1"

    def test_has_no_storage_object_id_field(self) -> None:
        """Storage is consumed within Tx1 and wrapped into the Blob -- there
        is no standalone Storage object ID to report."""
        field_names = {f.name for f in dataclasses.fields(Registration)}
        assert "storage_object_id" not in field_names

    def test_is_frozen(self) -> None:
        """Registration is immutable."""
        registration = self._registration()
        with pytest.raises(dataclasses.FrozenInstanceError):
            registration.object_id = "0xother"  # type: ignore[misc]

    def test_is_keyword_only(self) -> None:
        """Positional construction is rejected."""
        with pytest.raises(TypeError):
            Registration("0xblob", _BLOB_ID, 42, True, "0xdigest1")  # type: ignore[misc]

    def test_missing_required_field_raises(self) -> None:
        """A missing required keyword argument raises TypeError."""
        with pytest.raises(TypeError):
            Registration(  # type: ignore[call-arg]
                object_id="0xblob", blob_id=_BLOB_ID, end_epoch=42, deletable=True
            )


class TestCertifyResult:
    """Construction, immutability, and keyword-only enforcement."""

    def _result(self) -> CertifyResult:
        return CertifyResult(
            object_id="0xblob",
            blob_id=_BLOB_ID,
            certified=True,
            digest="0xdigest2",
        )

    def test_construction(self) -> None:
        """All fields round-trip as given."""
        result = self._result()
        assert result.object_id == "0xblob"
        assert result.blob_id == _BLOB_ID
        assert result.certified is True
        assert result.digest == "0xdigest2"

    def test_is_frozen(self) -> None:
        """CertifyResult is immutable."""
        result = self._result()
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.certified = False  # type: ignore[misc]

    def test_is_keyword_only(self) -> None:
        """Positional construction is rejected."""
        with pytest.raises(TypeError):
            CertifyResult("0xblob", _BLOB_ID, True, "0xdigest2")  # type: ignore[misc]

    def test_missing_required_field_raises(self) -> None:
        """A missing required keyword argument raises TypeError."""
        with pytest.raises(TypeError):
            CertifyResult(object_id="0xblob", blob_id=_BLOB_ID, certified=True)  # type: ignore[call-arg]


class TestEncodedStorageAmount:
    """``_encoded_storage_amount`` computes ``reserve_space``'s
    ``storage_amount`` via :func:`~pytusk.core.encoding.encoded_blob_length`.
    """

    def _encoded(self, *, unencoded_length: int, n_shards: int) -> EncodedBlob:
        """Build a minimal EncodedBlob for a given length/shard count."""
        return EncodedBlob(
            blob_id=_BLOB_ID,
            root_hash=_BLOB_ID,
            unencoded_length=unencoded_length,
            n_shards=n_shards,
            slivers=(),
            metadata_bcs=b"",
        )

    def test_delegates_to_encoded_blob_length(self) -> None:
        """The returned value matches encoded_blob_length for the same inputs."""
        encoded = self._encoded(unencoded_length=113, n_shards=10)
        assert _encoded_storage_amount(encoded=encoded) == encoded_blob_length(
            unencoded_length=113, n_shards=10
        )

    def test_n_shards_1000(self) -> None:
        """A representative mainnet-scale shard count matches the
        redstuff.move-derived value."""
        encoded = self._encoded(unencoded_length=1, n_shards=1000)
        assert _encoded_storage_amount(encoded=encoded) == 66_034_000


class TestEndEpochAndDeletable:
    """Field extraction from a freshly created Blob object's JSON view.

    Exercises ``blob_deletable_and_end_epoch``, which Tx1's read-back calls.
    A private copy lived in ``blob_execute`` and returned the same two values
    in the opposite order; it was consolidated away at Plan #28 step 11, and
    these unpacks flipped with it.
    """

    def test_happy_path(self) -> None:
        """end_epoch and deletable are read from the nested storage/deletable fields."""
        deletable, end_epoch = blob_deletable_and_end_epoch(obj=_blob_object())  # type: ignore[arg-type]
        assert end_epoch == 42
        assert deletable is True

    def test_deletable_false(self) -> None:
        """A permanent (non-deletable) blob's flag round-trips as False."""
        deletable, _ = blob_deletable_and_end_epoch(  # type: ignore[arg-type]
            obj=_blob_object(deletable=False)
        )
        assert deletable is False

    def test_no_json_view_raises(self) -> None:
        """A response with no JSON view raises ValueError rather than being
        silently treated as ineligible."""
        obj = _FakeObject(object_id="0xblob", json=None)
        with pytest.raises(ValueError):
            blob_deletable_and_end_epoch(obj=obj)  # type: ignore[arg-type]

    def test_missing_storage_field_raises(self) -> None:
        """A missing 'storage' field raises ValueError."""
        obj = _FakeObject(
            object_id="0xblob",
            json=_FakeJson(
                struct_value=_FakeStruct(fields={"deletable": _FakeValue(bool_value=True)})
            ),
        )
        with pytest.raises(ValueError):
            blob_deletable_and_end_epoch(obj=obj)  # type: ignore[arg-type]

    def test_missing_end_epoch_raises(self) -> None:
        """A 'storage' struct with no 'end_epoch' entry raises ValueError."""
        obj = _FakeObject(
            object_id="0xblob",
            json=_FakeJson(
                struct_value=_FakeStruct(
                    fields={
                        "storage": _FakeValue(struct_value=_FakeStruct(fields={})),
                        "deletable": _FakeValue(bool_value=True),
                    }
                )
            ),
        )
        with pytest.raises(ValueError):
            blob_deletable_and_end_epoch(obj=obj)  # type: ignore[arg-type]

    def test_missing_deletable_raises(self) -> None:
        """A missing 'deletable' field raises ValueError."""
        obj = _blob_object(deletable=None)
        with pytest.raises(ValueError):
            blob_deletable_and_end_epoch(obj=obj)  # type: ignore[arg-type]


class TestFindCreatedObjectId:
    """Locating the single object created and owned by a given address in effects."""

    def test_finds_created_and_owned(self) -> None:
        """A CREATED entry owned by the target address is returned."""
        effects = _FakeEffects(
            changed_objects=[
                _FakeChangedObject(
                    object_id="0xgas",
                    id_operation=sui_prot.ChangedObjectIdOperation.NONE,
                    output_owner=_FakeOwner(address="0xsender"),
                ),
                _FakeChangedObject(
                    object_id="0xblob",
                    id_operation=sui_prot.ChangedObjectIdOperation.CREATED,
                    output_owner=_FakeOwner(address="0xsender"),
                ),
            ]
        )
        assert (
            find_created_object_id(effects=effects, owner="0xsender")  # type: ignore[arg-type]
            == "0xblob"
        )

    def test_ignores_created_owned_by_someone_else(self) -> None:
        """A CREATED entry owned by a different address is not a match."""
        effects = _FakeEffects(
            changed_objects=[
                _FakeChangedObject(
                    object_id="0xother",
                    id_operation=sui_prot.ChangedObjectIdOperation.CREATED,
                    output_owner=_FakeOwner(address="0xsomeone_else"),
                ),
            ]
        )
        with pytest.raises(RuntimeError):
            find_created_object_id(effects=effects, owner="0xsender")  # type: ignore[arg-type]

    def test_no_changed_objects_raises(self) -> None:
        """An empty changed_objects list raises RuntimeError."""
        with pytest.raises(RuntimeError):
            find_created_object_id(effects=_FakeEffects(), owner="0xsender")  # type: ignore[arg-type]


class TestFindCreatedSharedObjectId:
    """Locating the single object created and shared, matched by type."""

    def test_finds_created_and_shared(self) -> None:
        """A CREATED, SHARED-kind entry matching the type substring is returned."""
        effects = _FakeEffects(
            changed_objects=[
                _FakeChangedObject(
                    object_id="0xgas",
                    id_operation=sui_prot.ChangedObjectIdOperation.NONE,
                    output_owner=_FakeOwner(address="0xsender"),
                ),
                _FakeChangedObject(
                    object_id="0xshared",
                    id_operation=sui_prot.ChangedObjectIdOperation.CREATED,
                    output_owner=_FakeOwner(kind=sui_prot.OwnerOwnerKind.SHARED),
                    object_type="0xpkg::shared_blob::SharedBlob",
                ),
            ]
        )
        assert (
            find_created_shared_object_id(
                effects=effects, object_type_substring="shared_blob::SharedBlob"
            )  # type: ignore[arg-type]
            == "0xshared"
        )

    def test_ignores_address_owned_created_object(self) -> None:
        """A CREATED entry that is ADDRESS-owned (not SHARED) is not a match."""
        effects = _FakeEffects(
            changed_objects=[
                _FakeChangedObject(
                    object_id="0xblob",
                    id_operation=sui_prot.ChangedObjectIdOperation.CREATED,
                    output_owner=_FakeOwner(address="0xsender"),
                    object_type="0xpkg::shared_blob::SharedBlob",
                ),
            ]
        )
        with pytest.raises(RuntimeError):
            find_created_shared_object_id(
                effects=effects, object_type_substring="shared_blob::SharedBlob"
            )  # type: ignore[arg-type]

    def test_ignores_mismatched_object_type(self) -> None:
        """A CREATED, SHARED-kind entry whose type doesn't match is not a match."""
        effects = _FakeEffects(
            changed_objects=[
                _FakeChangedObject(
                    object_id="0xother",
                    id_operation=sui_prot.ChangedObjectIdOperation.CREATED,
                    output_owner=_FakeOwner(kind=sui_prot.OwnerOwnerKind.SHARED),
                    object_type="0xpkg::other::OtherShared",
                ),
            ]
        )
        with pytest.raises(RuntimeError):
            find_created_shared_object_id(
                effects=effects, object_type_substring="shared_blob::SharedBlob"
            )  # type: ignore[arg-type]

    def test_no_changed_objects_raises(self) -> None:
        """An empty changed_objects list raises RuntimeError."""
        with pytest.raises(RuntimeError):
            find_created_shared_object_id(
                effects=_FakeEffects(), object_type_substring="shared_blob::SharedBlob"
            )  # type: ignore[arg-type]


class TestRequireSuccess:
    """Status checking on a submitted transaction's effects."""

    def test_success_returns_effects(self) -> None:
        """A successful status returns the effects object unchanged."""
        effects = _FakeEffects(status=_FakeStatus(success=True))
        result_data = _FakeResultData(effects=effects)
        assert (
            require_success(result_data=result_data, label="test")  # type: ignore[arg-type]
            is effects
        )

    def test_failure_raises_with_description(self) -> None:
        """An on-chain abort raises RuntimeError carrying the error description."""
        effects = _FakeEffects(
            status=_FakeStatus(
                success=False, error=_FakeExecutionError(description="boom")
            )
        )
        result_data = _FakeResultData(effects=effects)
        with pytest.raises(RuntimeError, match="boom"):
            require_success(result_data=result_data, label="test")  # type: ignore[arg-type]

    def test_failure_without_error_uses_unknown(self) -> None:
        """An abort with no error detail falls back to 'unknown error'."""
        effects = _FakeEffects(status=_FakeStatus(success=False, error=None))
        result_data = _FakeResultData(effects=effects)
        with pytest.raises(RuntimeError, match="unknown error"):
            require_success(result_data=result_data, label="test")  # type: ignore[arg-type]

    def test_no_effects_raises(self) -> None:
        """A response carrying no effects at all raises RuntimeError."""
        result_data = _FakeResultData(effects=None)
        with pytest.raises(RuntimeError):
            require_success(result_data=result_data, label="test")  # type: ignore[arg-type]


class _FakeFinalityClient:
    """Fake ``WalrusClient``-shaped object for ``wait_for_finality`` polling.

    Implements only ``execute(command=...)``, the subset ``wait_for_finality``
    uses. Each call returns the next ``SuiRpcResult`` from ``responses``, in
    order; every dispatched command is recorded in ``calls`` for assertions.
    """

    def __init__(self, *, responses: list[SuiRpcResult]) -> None:
        self.responses = list(responses)
        self.calls: list[object] = []

    async def execute(self, *, command: object) -> SuiRpcResult:
        """Record the dispatched command and return the next canned response."""
        self.calls.append(command)
        return self.responses[len(self.calls) - 1]


class TestWaitForFinality:
    """Polling behaviour of ``wait_for_finality`` against a faked client.

    ``asyncio.sleep`` is monkeypatched out (as referenced from
    ``pytusk.core.ops.system_reads``, i.e. its plain ``import asyncio``) so
    these tests run fast and can assert exactly how many times -- and after
    which attempts -- a delay was taken.
    """

    def _patch_sleep(self, *, monkeypatch: pytest.MonkeyPatch) -> list[float]:
        """Replace ``system_reads.asyncio.sleep`` with a recorder and return
        the list it appends delays to."""
        sleep_calls: list[float] = []

        async def _fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        monkeypatch.setattr(system_reads.asyncio, "sleep", _fake_sleep)
        return sleep_calls

    async def test_visible_immediately(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A first poll that is already ok with non-None, checkpointed
        result_data returns True after exactly one call, with no sleep
        taken."""
        sleep_calls = self._patch_sleep(monkeypatch=monkeypatch)
        client = _FakeFinalityClient(
            responses=[SuiRpcResult(True, "", types.SimpleNamespace(checkpoint=42))]
        )

        visible = await wait_for_finality(client=client, digest="0xdigest")  # type: ignore[arg-type]

        assert visible is True
        assert len(client.calls) == 1
        assert sleep_calls == []

    async def test_visible_after_polls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two not-visible polls -- one not-ok, one ok with result_data still
        None (the real-world pre-checkpoint race) -- followed by a visible,
        checkpointed third poll returns True after exactly three calls."""
        sleep_calls = self._patch_sleep(monkeypatch=monkeypatch)
        client = _FakeFinalityClient(
            responses=[
                SuiRpcResult(False, "not found", None),
                SuiRpcResult(True, "", None),
                SuiRpcResult(True, "", types.SimpleNamespace(checkpoint=42)),
            ]
        )

        visible = await wait_for_finality(client=client, digest="0xdigest")  # type: ignore[arg-type]

        assert visible is True
        assert len(client.calls) == 3
        assert len(sleep_calls) == 2

    async def test_found_but_checkpoint_pending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A poll where the digest is found (result_data is not None) but
        ``checkpoint`` is still falsy -- the transaction is visible by
        digest lookup but has not yet landed in a checkpoint -- must NOT
        be treated as finalized. A subsequent poll with a truthy
        ``checkpoint`` returns True."""
        sleep_calls = self._patch_sleep(monkeypatch=monkeypatch)
        client = _FakeFinalityClient(
            responses=[
                SuiRpcResult(True, "", types.SimpleNamespace(checkpoint=None)),
                SuiRpcResult(True, "", types.SimpleNamespace(checkpoint=42)),
            ]
        )

        visible = await wait_for_finality(client=client, digest="0xdigest")  # type: ignore[arg-type]

        assert visible is True
        assert len(client.calls) == 2
        assert len(sleep_calls) == 1

    async def test_never_checkpointed_budget_exhausted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression test: a digest that is found on every poll (result_data
        is never None) but never reaches ``checkpoint`` inclusion must
        return False after ``max_attempts`` -- being found by digest alone
        is not finality."""
        sleep_calls = self._patch_sleep(monkeypatch=monkeypatch)
        client = _FakeFinalityClient(
            responses=[
                SuiRpcResult(True, "", types.SimpleNamespace(checkpoint=None))
            ]
            * 3
        )

        visible = await wait_for_finality(
            client=client, digest="0xdigest", max_attempts=3  # type: ignore[arg-type]
        )

        assert visible is False
        assert len(client.calls) == 3
        assert len(sleep_calls) == 2

    async def test_budget_exhausted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When every poll within ``max_attempts`` is not-visible,
        ``wait_for_finality`` returns False after exactly ``max_attempts``
        calls and sleeps only ``max_attempts - 1`` times -- no trailing
        sleep after the final, unproductive attempt."""
        sleep_calls = self._patch_sleep(monkeypatch=monkeypatch)
        client = _FakeFinalityClient(
            responses=[SuiRpcResult(False, "not found", None)] * 3
        )

        visible = await wait_for_finality(
            client=client, digest="0xdigest", max_attempts=3  # type: ignore[arg-type]
        )

        assert visible is False
        assert len(client.calls) == 3
        assert len(sleep_calls) == 2


class _RecordingTxn:
    """Minimal recording fake for ``AsyncSuiTransaction``, capturing every
    ``move_call``/``transfer_objects`` call, in order, in ``calls``.

    Only the two methods PTB composition here actually invokes are
    implemented, plus ``build_and_sign`` (a no-op returning an empty
    txdict) for ``TestExecuteReserveAndRegisterTransfersToSender`` below,
    which drives the real ``execute_reserve_and_register`` wrapper far
    enough to reach it. Each ``move_call`` returns a distinct sentinel
    string (``"result1"``, ``"result2"``, ...) recorded alongside the call
    under the ``"result"`` key, so a later ``transfer_objects``/
    ``register_blob`` argument can be asserted to be the SAME object a
    specific earlier ``move_call`` produced.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._next_result = 0

    async def move_call(
        self,
        *,
        target: str,
        arguments: list[object],
        type_arguments: list[object],
    ) -> str:
        """Record the call and return a distinct per-call sentinel result."""
        self._next_result += 1
        result = f"result{self._next_result}"
        self.calls.append(
            (
                "move_call",
                {
                    "target": target,
                    "arguments": arguments,
                    "type_arguments": type_arguments,
                    "result": result,
                },
            )
        )
        return result

    async def transfer_objects(
        self, *, transfers: list[object], recipient: str
    ) -> None:
        """Record the call; no return value, matching the real signature."""
        self.calls.append(
            ("transfer_objects", {"transfers": transfers, "recipient": recipient})
        )

    async def build_and_sign(self) -> dict[str, object]:
        """Return an empty txdict; only used by tests that need to reach
        past PTB composition into ``execute_reserve_and_register``'s own
        submission code."""
        return {}


def _certificate() -> Certificate:
    """A minimal, well-formed Certificate fixture; add_certify only reads
    its three wire-format fields verbatim and never validates them."""
    return Certificate(
        serialized_message=b"msg",
        aggregate_signature=b"sig",
        signers_bitmap=b"\x01",
        signer_positions=(0,),
        weight=1,
    )


class TestAddCertifyTransferOrdering:
    """``add_certify`` is pure PTB composition: the ``certify_blob``
    move_call is always added; a ``transfer_objects`` command is added
    ONLY when ``recipient`` is given, and -- when given -- strictly AFTER
    the move_call, since certification must precede the handover in the
    same PTB."""

    async def test_no_recipient_omits_transfer(self) -> None:
        """With ``recipient=None`` (the default), only the ``certify_blob``
        move_call is added; no ``transfer_objects`` call is made."""
        txn = _RecordingTxn()
        certificate = _certificate()

        await add_certify(
            txn=txn,  # type: ignore[arg-type]
            package_id="0xpkg",
            system_object="0xsystem",
            blob_object_id="0xblob",
            certificate=certificate,
        )

        kinds = [kind for kind, _ in txn.calls]
        assert kinds == ["move_call"]
        _, move_call_kwargs = txn.calls[0]
        assert move_call_kwargs["target"] == "0xpkg::system::certify_blob"
        assert move_call_kwargs["arguments"] == [
            "0xsystem",
            "0xblob",
            certificate.aggregate_signature,
            certificate.signers_bitmap,
            certificate.serialized_message,
        ]

    async def test_recipient_given_adds_transfer_after_move_call(self) -> None:
        """With a ``recipient``, BOTH the ``certify_blob`` move_call and a
        ``transfer_objects(transfers=[blob_object_id], recipient=...)`` are
        added, with the transfer strictly AFTER the move_call."""
        txn = _RecordingTxn()
        certificate = _certificate()
        recipient = "0x" + "ab" * 32

        await add_certify(
            txn=txn,  # type: ignore[arg-type]
            package_id="0xpkg",
            system_object="0xsystem",
            blob_object_id="0xblob",
            certificate=certificate,
            recipient=recipient,
        )

        kinds = [kind for kind, _ in txn.calls]
        assert kinds == ["move_call", "transfer_objects"]
        _, transfer_kwargs = txn.calls[1]
        assert transfer_kwargs["transfers"] == ["0xblob"]
        assert transfer_kwargs["recipient"] == recipient


class TestExecuteReserveAndRegisterNoRecipientParam:
    """Regression guard: Tx1 always transfers the newly registered Blob to
    the resolved sender (see ``execute_reserve_and_register``'s docstring)
    -- handing it to a different recipient is Tx2's job (``add_certify``'s
    ``recipient``), not Tx1's. ``execute_reserve_and_register`` must
    therefore not accept a ``recipient`` keyword at all."""

    async def test_recipient_kwarg_rejected(self) -> None:
        """Passing ``recipient=`` raises ``TypeError`` -- Python rejects the
        unexpected keyword argument while binding the call, before any of
        the function's own body (and therefore no network call) runs."""
        with pytest.raises(TypeError):
            await execute_reserve_and_register(
                client=object(),  # type: ignore[arg-type]
                encoded=object(),  # type: ignore[arg-type]
                epochs=1,
                deletable=False,
                package_id="0xpkg",
                system_object="0xsystem",
                recipient="0xrecipient",  # type: ignore[call-arg]
            )


class _FakePysuiConfig:
    """Stand-in for ``WalrusClient.pysui_client.config``, exposing only
    ``active_address`` -- the sole attribute
    ``execute_reserve_and_register`` reads from it."""

    def __init__(self, *, active_address: str) -> None:
        self.active_address = active_address


class _FakePysuiClient:
    """Stand-in for ``WalrusClient.pysui_client``, exposing only
    ``.config.active_address``."""

    def __init__(self, *, active_address: str) -> None:
        self.config = _FakePysuiConfig(active_address=active_address)


class _FakeReserveClient:
    """Minimal ``WalrusClient``-shaped fake for driving
    ``execute_reserve_and_register`` far enough to observe its own
    sender-transfer call, without needing a live node.

    ``.pysui_client.config.active_address`` supplies the default sender
    when the caller omits ``sender=``. ``.transaction()`` returns the given
    recording ``txn`` fake. ``.execute()`` always reports failure via
    ``SuiRpcResult(False, ...)``, which makes
    ``execute_reserve_and_register`` raise ``RuntimeError`` immediately
    after checking the (monkeypatched) ``ExecuteTransaction`` submission
    result -- exactly where this fake wants execution to stop: after
    ``add_reserve_and_register`` and the transfer-to-sender call under test
    have already run, before any of the read-back machinery
    (``find_created_object_id``, ``wait_for_finality``, ``GetObject``) that
    would otherwise require the "elaborate mock" this module's docstring
    deliberately avoids.
    """

    def __init__(self, *, txn: _RecordingTxn, active_address: str) -> None:
        self._txn = txn
        self.pysui_client = _FakePysuiClient(active_address=active_address)

    async def transaction(
        self, *, initial_sender: str, initial_sponsor: str | None
    ) -> _RecordingTxn:
        """Return the pre-built recording fake regardless of arguments."""
        return self._txn

    async def execute(self, *, command: object) -> SuiRpcResult:
        """Always report submission failure, stopping the wrapper right
        after the transfer-to-sender call under test."""
        return SuiRpcResult(False, "stub failure -- test stops here by design")


class TestExecuteReserveAndRegisterTransfersToSender:
    """Tx1 always transfers the newly registered Blob to the RESOLVED
    sender -- never to an arbitrary recipient -- see
    ``execute_reserve_and_register``'s docstring."""

    def _encoded(self) -> EncodedBlob:
        """A minimal EncodedBlob fixture; only the fields
        ``add_reserve_and_register`` reads are populated with real values."""
        return EncodedBlob(
            blob_id=_BLOB_ID,
            root_hash=_BLOB_ID,
            unencoded_length=113,
            n_shards=7,
            slivers=(),
            metadata_bcs=b"",
        )

    async def test_transfer_targets_explicit_sender(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ``sender`` is given explicitly, the transfer targets it
        verbatim, and carries the exact ``Blob`` result
        ``add_reserve_and_register`` produced."""
        txn = _RecordingTxn()
        client = _FakeReserveClient(txn=txn, active_address="0xactive")
        monkeypatch.setattr(
            blob_execute, "ExecuteTransaction", lambda **kwargs: object()
        )

        with pytest.raises(RuntimeError, match="stub failure"):
            await execute_reserve_and_register(
                client=client,  # type: ignore[arg-type]
                encoded=self._encoded(),
                epochs=3,
                deletable=False,
                package_id="0xpkg",
                system_object="0xsystem",
                payment_coin="0xcoin",
                sender="0xexplicit_sender",
            )

        transfer_calls = [
            kwargs for kind, kwargs in txn.calls if kind == "transfer_objects"
        ]
        assert len(transfer_calls) == 1
        assert transfer_calls[0]["recipient"] == "0xexplicit_sender"
        register_blob_result = txn.calls[-2][1]["result"]
        assert transfer_calls[0]["transfers"] == [register_blob_result]

    async def test_transfer_targets_active_address_when_sender_omitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ``sender`` is omitted, the transfer targets the client's
        resolved active address instead."""
        txn = _RecordingTxn()
        client = _FakeReserveClient(txn=txn, active_address="0xactive")
        monkeypatch.setattr(
            blob_execute, "ExecuteTransaction", lambda **kwargs: object()
        )

        with pytest.raises(RuntimeError, match="stub failure"):
            await execute_reserve_and_register(
                client=client,  # type: ignore[arg-type]
                encoded=self._encoded(),
                epochs=3,
                deletable=False,
                package_id="0xpkg",
                system_object="0xsystem",
                payment_coin="0xcoin",
            )

        transfer_calls = [
            kwargs for kind, kwargs in txn.calls if kind == "transfer_objects"
        ]
        assert len(transfer_calls) == 1
        assert transfer_calls[0]["recipient"] == "0xactive"


class _FakeReserveClientWithSponsorCheck(_FakeReserveClient):
    """``_FakeReserveClient`` plus a ``keypair_for_address`` on its
    ``pysui_client.config``, so a test can prove ``execute_reserve_and_register``
    does NOT consult it -- see
    ``TestExecuteReserveAndRegisterSponsorPreflightSkipped``."""

    def __init__(
        self, *, txn: _RecordingTxn, active_address: str, signable: tuple[str, ...] = ()
    ) -> None:
        super().__init__(txn=txn, active_address=active_address)
        self.pysui_client.config = _FakePreflightConfig(signable=signable)  # type: ignore[assignment]
        self.pysui_client.config.active_address = active_address  # type: ignore[attr-defined]


class TestExecuteReserveAndRegisterSponsorPreflightSkipped:
    """``execute_reserve_and_register`` deliberately does NOT call
    ``preflight_sponsor``. A legitimate sponsored-transaction pattern has
    the sponsor signing OUT-OF-BAND, with no keypair in the local
    ``PysuiConfiguration`` -- this Tx1 is built for the caller to sign (or
    hand to the sponsor for external signing), unlike the relay pipeline's
    Tx2, which is auto-signed internally and therefore DOES need
    ``preflight_sponsor`` to fail fast (see
    :mod:`pytusk.core.pipelines.write`'s ``store_blob_relay`` and
    ``TestPreflightSponsor``/``test_relay_pipeline.py``'s
    ``test_unsignable_sponsor_builds_nothing``). Forcing the signability
    check here would break the out-of-band pattern, so a sponsor with NO
    local keypair must proceed exactly as a signable one would -- do not
    "fix" this by reintroducing a ``preflight_sponsor`` call in this
    function."""

    def _encoded(self) -> EncodedBlob:
        return EncodedBlob(
            blob_id=_BLOB_ID,
            root_hash=_BLOB_ID,
            unencoded_length=113,
            n_shards=7,
            slivers=(),
            metadata_bcs=b"",
        )

    async def test_unsignable_sponsor_still_proceeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sponsor absent from local config (``signable=()``, so
        ``keypair_for_address`` would raise ``ValueError`` if it were ever
        consulted) must NOT be rejected: the wrapper proceeds all the way
        to opening and building Tx1, only stopping at the fake client's
        canned submission failure -- proof no sponsor-signability check
        ran anywhere in this path."""
        txn = _RecordingTxn()
        client = _FakeReserveClientWithSponsorCheck(
            txn=txn, active_address="0xactive"
        )
        monkeypatch.setattr(
            blob_execute, "ExecuteTransaction", lambda **kwargs: object()
        )
        with pytest.raises(RuntimeError, match="stub failure"):
            await execute_reserve_and_register(
                client=client,  # type: ignore[arg-type]
                encoded=self._encoded(),
                epochs=3,
                deletable=False,
                package_id="0xpkg",
                system_object="0xsystem",
                payment_coin="0xcoin",
                sponsor="0xstranger",
            )
        assert any(kind == "transfer_objects" for kind, _ in txn.calls)


class _RecordingTipTxn:
    """Recording fake for ``AsyncSuiTransaction`` covering everything
    ``add_registration_sequence`` can compose: a REAL
    ``ProgrammableTransactionBuilder`` (so ``add_tip``'s input-0 guard and
    pure-input registration are exercised for real, exactly like
    ``test_relay_tip_payment.py``'s ``_FakeTxn``), plus recording
    ``split_coin``/``move_call``/``transfer_objects`` stubs (so
    ``add_reserve_and_register``'s move_call ordering is exercised for
    real, exactly like ``_RecordingTxn`` above). Every command -- whichever
    method produced it -- is appended to ``calls`` in the SAME list, in
    call order, so the full Tx1 command sequence (tip commands interleaved
    with reserve/register/transfer) can be asserted as one timeline.
    """

    def __init__(self) -> None:
        self.builder = ProgrammableTransactionBuilder()
        self.gas = "GAS-SENTINEL"
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._next_result = 0

    def _result(self) -> str:
        self._next_result += 1
        return f"result{self._next_result}"

    async def split_coin(self, *, coin: Any, amounts: list[int]) -> str:
        """Record the split and return a distinct sentinel result."""
        result = self._result()
        self.calls.append(
            ("split_coin", {"coin": coin, "amounts": amounts, "result": result})
        )
        return result

    async def move_call(
        self,
        *,
        target: str,
        arguments: list[object],
        type_arguments: list[object],
    ) -> str:
        """Record the call and return a distinct sentinel result."""
        result = self._result()
        self.calls.append(
            (
                "move_call",
                {
                    "target": target,
                    "arguments": arguments,
                    "type_arguments": type_arguments,
                    "result": result,
                },
            )
        )
        return result

    async def transfer_objects(
        self, *, transfers: list[object], recipient: str
    ) -> None:
        """Record the transfer; no return value, matching the real signature."""
        self.calls.append(
            ("transfer_objects", {"transfers": transfers, "recipient": recipient})
        )


def _registration_encoded() -> EncodedBlob:
    """A minimal EncodedBlob fixture; only the fields
    ``add_reserve_and_register`` reads are populated with real values."""
    return EncodedBlob(
        blob_id=_BLOB_ID,
        root_hash=_BLOB_ID,
        unencoded_length=113,
        n_shards=7,
        slivers=(),
        metadata_bcs=b"",
    )


def _tip_composition(*, payment_coin: str = FROM_GAS) -> TipComposition:
    """A minimal, well-formed TipComposition fixture."""
    return TipComposition(
        relay_address="0xrelay",
        tip_amount=1000,
        auth_package=build_auth_package(data=b"payload"),
        payment_coin=payment_coin,
    )


class TestAddRegistrationSequenceTipPlacement:
    """When a tip is supplied, ``add_tip``'s input-0 precondition must hold
    -- the auth package must land as PTB input zero, byte-exact, before
    anything else touches the transaction."""

    async def test_auth_package_is_input_zero(self) -> None:
        txn = _RecordingTipTxn()
        tip = _tip_composition()

        await add_registration_sequence(
            txn=txn,  # type: ignore[arg-type]
            encoded=_registration_encoded(),
            epochs=3,
            deletable=False,
            package_id="0xpkg",
            system_object="0xsystem",
            recipient="0xrecipient",
            wal_payment_coin="0xwal",
            tip=tip,
        )

        inputs = list(txn.builder.inputs.values())
        assert inputs[0].enum_name == "Pure"
        assert inputs[0].value == list(tip.auth_package.bcs)
        assert len(inputs[0].value) == 72

    async def test_no_tip_registers_no_input(self) -> None:
        """Without a tip, ``add_reserve_and_register`` itself never
        registers a pure input -- its arguments are all object ids/ints/
        bools threaded as move_call arguments, not PTB inputs."""
        txn = _RecordingTipTxn()

        await add_registration_sequence(
            txn=txn,  # type: ignore[arg-type]
            encoded=_registration_encoded(),
            epochs=3,
            deletable=False,
            package_id="0xpkg",
            system_object="0xsystem",
            recipient="0xrecipient",
            wal_payment_coin="0xwal",
            tip=None,
        )

        assert list(txn.builder.inputs.values()) == []


class TestAddRegistrationSequenceCommandParity:
    """The reserve/register/transfer command sequence must be identical
    with and without a tip -- a tip only PREPENDS its own split+transfer,
    it must never change what ``add_reserve_and_register`` or the final
    transfer does."""

    async def test_tip_only_prepends_its_own_two_commands(self) -> None:
        txn_no_tip = _RecordingTipTxn()
        await add_registration_sequence(
            txn=txn_no_tip,  # type: ignore[arg-type]
            encoded=_registration_encoded(),
            epochs=3,
            deletable=False,
            package_id="0xpkg",
            system_object="0xsystem",
            recipient="0xrecipient",
            wal_payment_coin="0xwal",
            tip=None,
        )

        txn_with_tip = _RecordingTipTxn()
        await add_registration_sequence(
            txn=txn_with_tip,  # type: ignore[arg-type]
            encoded=_registration_encoded(),
            epochs=3,
            deletable=False,
            package_id="0xpkg",
            system_object="0xsystem",
            recipient="0xrecipient",
            wal_payment_coin="0xwal",
            tip=_tip_composition(),
        )

        # The tip's own two commands (split_coin, transfer_objects to the
        # relay) come first, and only first.
        tip_head = txn_with_tip.calls[:2]
        assert [kind for kind, _ in tip_head] == ["split_coin", "transfer_objects"]

        # Everything after is the SAME shape as the untipped run.
        tipped_tail = txn_with_tip.calls[2:]
        assert [kind for kind, _ in tipped_tail] == [
            kind for kind, _ in txn_no_tip.calls
        ]
        assert [kind for kind, _ in tipped_tail] == [
            "move_call",
            "move_call",
            "transfer_objects",
        ]

        # reserve_space (fully literal arguments -- no result reference in
        # its own argument list) must match byte-for-byte.
        reserve_tip = tipped_tail[0][1]
        reserve_no_tip = txn_no_tip.calls[0][1]
        assert reserve_tip["target"] == reserve_no_tip["target"]
        assert reserve_tip["arguments"] == reserve_no_tip["arguments"]

        # register_blob matches on everything except the injected `storage`
        # result reference (index 1), which necessarily differs -- the
        # tipped run's reserve_space produced a later sentinel because the
        # tip's split_coin claimed the earlier ones.
        register_tip_args = list(tipped_tail[1][1]["arguments"])
        register_no_tip_args = list(txn_no_tip.calls[1][1]["arguments"])
        assert tipped_tail[1][1]["target"] == txn_no_tip.calls[1][1]["target"]
        assert register_tip_args[0] == register_no_tip_args[0]
        assert register_tip_args[2:] == register_no_tip_args[2:]
        assert register_tip_args[1] == reserve_tip["result"]
        assert register_no_tip_args[1] == reserve_no_tip["result"]

        # The final transfer targets the same recipient and consumes
        # whichever run's own register_blob result.
        assert tipped_tail[2][1]["recipient"] == txn_no_tip.calls[2][1]["recipient"]
        assert tipped_tail[2][1]["recipient"] == "0xrecipient"
        assert tipped_tail[2][1]["transfers"] == [tipped_tail[1][1]["result"]]
        assert txn_no_tip.calls[2][1]["transfers"] == [txn_no_tip.calls[1][1]["result"]]


class TestAddRegistrationSequenceAttributes:
    """``attributes`` composes one ``insert_or_update_metadata_pair`` per
    pair, against the registered ``Blob`` and before the transfer that
    consumes it."""

    async def test_omitted_composes_no_extra_commands(self) -> None:
        """The default writes no metadata commands at all."""
        txn = _RecordingTipTxn()

        await add_registration_sequence(
            txn=txn,  # type: ignore[arg-type]
            encoded=_registration_encoded(),
            epochs=3,
            deletable=False,
            package_id="0xpkg",
            system_object="0xsystem",
            recipient="0xrecipient",
            wal_payment_coin="0xwal",
            tip=None,
        )

        assert [kind for kind, _ in txn.calls] == [
            "move_call",
            "move_call",
            "transfer_objects",
        ]

    async def test_single_pair_targets_the_registered_blob(self) -> None:
        """The pair is written against ``register_blob``'s own command
        result -- not a fresh input -- and lands before the transfer."""
        txn = _RecordingTipTxn()

        await add_registration_sequence(
            txn=txn,  # type: ignore[arg-type]
            encoded=_registration_encoded(),
            epochs=3,
            deletable=False,
            package_id="0xpkg",
            system_object="0xsystem",
            recipient="0xrecipient",
            wal_payment_coin="0xwal",
            tip=None,
            attributes={"quilt": "v1"},
        )

        assert [kind for kind, _ in txn.calls] == [
            "move_call",
            "move_call",
            "move_call",
            "transfer_objects",
        ]

        register_result = txn.calls[1][1]["result"]
        attribute_call = txn.calls[2][1]
        assert (
            attribute_call["target"] == "0xpkg::blob::insert_or_update_metadata_pair"
        )
        assert list(attribute_call["arguments"]) == [register_result, "quilt", "v1"]
        assert attribute_call["type_arguments"] == []

        # The Blob is still live when the attribute is written, and is only
        # consumed afterwards.
        assert list(txn.calls[3][1]["transfers"]) == [register_result]

    async def test_multiple_pairs_compose_one_command_each_in_mapping_order(
        self,
    ) -> None:
        """Mapping order is preserved. These are INDEPENDENT PTB commands
        taking pure ``String`` arguments, not a BCS map, so the canonical
        key ordering that governs a serialized map is deliberately NOT
        imposed here -- a sort would reorder these to alpha, zeta."""
        txn = _RecordingTipTxn()

        await add_registration_sequence(
            txn=txn,  # type: ignore[arg-type]
            encoded=_registration_encoded(),
            epochs=3,
            deletable=False,
            package_id="0xpkg",
            system_object="0xsystem",
            recipient="0xrecipient",
            wal_payment_coin="0xwal",
            tip=None,
            attributes={"zeta": "last", "alpha": "first"},
        )

        assert [kind for kind, _ in txn.calls] == [
            "move_call",
            "move_call",
            "move_call",
            "move_call",
            "transfer_objects",
        ]

        register_result = txn.calls[1][1]["result"]
        attribute_args = [list(call["arguments"]) for _, call in txn.calls[2:4]]

        assert [(args[1], args[2]) for args in attribute_args] == [
            ("zeta", "last"),
            ("alpha", "first"),
        ]
        assert all(args[0] == register_result for args in attribute_args)


class _FakePreflightConfig:
    """Stand-in for ``WalrusClient.pysui_client.config``, controlling which
    addresses ``keypair_for_address`` treats as signable."""

    def __init__(self, *, signable: tuple[str, ...] = ()) -> None:
        self._signable = set(signable)

    def keypair_for_address(self, *, address: str) -> object:
        """Mirror pysui: raise ValueError when the address has no key."""
        if address not in self._signable:
            raise ValueError(f"Keypair for address: {address} does not exist.")
        return object()


class _FakePreflightClient:
    """Minimal ``WalrusClient``-shaped fake for ``preflight_sponsor``/
    ``preflight_payment``: ``.pysui_client.config.keypair_for_address`` for
    the sponsor check, and ``.execute()`` returning a canned coin object for
    the tip-coin check (via ``assert_coin_usable``'s ``GetObject``)."""

    def __init__(
        self,
        *,
        signable: tuple[str, ...] = (),
        coin_response: SuiRpcResult | None = None,
    ) -> None:
        self.pysui_client = types.SimpleNamespace(
            config=_FakePreflightConfig(signable=signable)
        )
        self._coin_response = coin_response

    async def execute(self, *, command: object) -> SuiRpcResult:
        """Return the canned coin-fetch response."""
        assert self._coin_response is not None, (
            "execute() was called but no coin_response was configured -- a "
            "test that expects the tip-coin check to be SKIPPED must not "
            "reach this."
        )
        return self._coin_response


def _preflight_coin(*, owner: str | None, balance: int | None) -> object:
    """Build a minimal fetched-object stub exposing owner and balance,
    matching what ``assert_coin_usable`` reads."""
    owner_msg = types.SimpleNamespace(address=owner) if owner is not None else None
    return types.SimpleNamespace(owner=owner_msg, balance=balance)


class TestPreflightSponsor:
    """``preflight_sponsor`` must raise the SAME error the pipeline raised
    before this function existed: a ``RelayUploadError`` naming the
    ``"preflight"`` stage, whenever ``sponsor`` is given and unsignable --
    and must be a no-op when ``sponsor`` is ``None``."""

    async def test_unsignable_sponsor_raises_relay_upload_error(self) -> None:
        client = _FakePreflightClient()
        with pytest.raises(RelayUploadError, match="not signable"):
            await preflight_sponsor(
                client=client,  # type: ignore[arg-type]
                sponsor="0xstranger",
            )

    async def test_unsignable_sponsor_reports_preflight_stage(self) -> None:
        client = _FakePreflightClient()
        with pytest.raises(RelayUploadError) as excinfo:
            await preflight_sponsor(
                client=client,  # type: ignore[arg-type]
                sponsor="0xstranger",
            )
        assert excinfo.value.stage == "preflight"

    async def test_signable_sponsor_proceeds(self) -> None:
        client = _FakePreflightClient(signable=("0xsponsor",))
        result = await preflight_sponsor(
            client=client,  # type: ignore[arg-type]
            sponsor="0xsponsor",
        )
        assert result is None

    async def test_none_sponsor_is_a_no_op(self) -> None:
        """``sponsor=None`` must never touch the client at all -- an
        unsignable client configuration must not raise when no sponsor is
        given."""
        client = _FakePreflightClient()
        result = await preflight_sponsor(
            client=client,  # type: ignore[arg-type]
            sponsor=None,
        )
        assert result is None


class TestPreflightPaymentTipCoinCheck:
    """``preflight_payment`` must raise the SAME error
    ``assert_coin_usable`` always raised for an unusable tip coin, and must
    skip the check entirely for ``FROM_GAS``/``None``."""

    async def test_unusable_tip_coin_raises(self) -> None:
        client = _FakePreflightClient(
            coin_response=SuiRpcResult(
                True, "", _preflight_coin(owner="0xother", balance=100)
            )
        )
        with pytest.raises(RuntimeError, match="not by any of"):
            await preflight_payment(
                client=client,  # type: ignore[arg-type]
                sender="0xsender",
                tip_source="0xcoin",
                tip_minimum_balance=50,
                wal_payment_coin="0xwal",
            )

    async def test_insufficient_balance_raises(self) -> None:
        client = _FakePreflightClient(
            coin_response=SuiRpcResult(
                True, "", _preflight_coin(owner="0xsender", balance=10)
            )
        )
        with pytest.raises(RuntimeError, match="below the required"):
            await preflight_payment(
                client=client,  # type: ignore[arg-type]
                sender="0xsender",
                tip_source="0xcoin",
                tip_minimum_balance=50,
                wal_payment_coin="0xwal",
            )

    async def test_usable_coin_passes(self) -> None:
        client = _FakePreflightClient(
            coin_response=SuiRpcResult(
                True, "", _preflight_coin(owner="0xsender", balance=50)
            )
        )
        result = await preflight_payment(
            client=client,  # type: ignore[arg-type]
            sender="0xsender",
            tip_source="0xcoin",
            tip_minimum_balance=50,
            wal_payment_coin="0xwal",
        )
        assert result == "0xwal"

    async def test_from_gas_skips_the_check(self) -> None:
        client = _FakePreflightClient()  # would AssertionError if execute() ran
        result = await preflight_payment(
            client=client,  # type: ignore[arg-type]
            sender="0xsender",
            tip_source=FROM_GAS,
            wal_payment_coin="0xwal",
        )
        assert result == "0xwal"

    async def test_none_skips_the_check(self) -> None:
        client = _FakePreflightClient()  # would AssertionError if execute() ran
        result = await preflight_payment(
            client=client,  # type: ignore[arg-type]
            sender="0xsender",
            tip_source=None,
            wal_payment_coin="0xwal",
        )
        assert result == "0xwal"


class TestPreflightPaymentWalCoinResolution:
    """WAL payment coin resolution: verbatim when given, else selected via
    ``select_wal_payment_coin``."""

    async def test_explicit_coin_returned_verbatim(self) -> None:
        client = _FakePreflightClient()
        result = await preflight_payment(
            client=client,  # type: ignore[arg-type]
            sender="0xsender",
            wal_payment_coin="0xexplicit",
        )
        assert result == "0xexplicit"

    async def test_omitted_coin_falls_back_to_select(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _select(**kwargs: Any) -> str:
            return "0xauto"

        monkeypatch.setattr(blob_execute, "select_wal_payment_coin", _select)
        client = _FakePreflightClient()
        result = await preflight_payment(
            client=client,  # type: ignore[arg-type]
            sender="0xsender",
        )
        assert result == "0xauto"
