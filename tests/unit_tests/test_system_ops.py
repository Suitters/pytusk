#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for ``pytusk.core.system_ops``.

Scope, deliberately narrow: this module's real work is building and
submitting PTBs against a live pysui client (``execute_reserve_and_register``,
``execute_certify``, ``resolve_package_id``, ``select_wal_payment_coin``).
None of that is meaningfully unit-testable without either a live node or an
elaborate mock of ``AsyncSuiTransaction``/``WalrusClient`` that would fake
coverage rather than prove behaviour -- so none of it is tested here. What IS
tested: the pure/local helpers that do not require a client at all
(``_end_epoch_and_deletable``, ``_find_created_object_id``,
``_require_success``, the ``_encoded_storage_amount`` blocker) and the
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

import pysui.sui.sui_grpc.suimsgs.sui.rpc.v2 as sui_prot
import pytest
from pysui import SuiRpcResult

from pytusk.core import system_ops
from pytusk.core.certification import Certificate
from pytusk.core.encoding import EncodedBlob, encoded_blob_length
from pytusk.core.system_ops import (
    CertifyResult,
    Registration,
    _encoded_storage_amount,
    _end_epoch_and_deletable,
    _find_created_object_id,
    _require_success,
    add_certify,
    execute_reserve_and_register,
    wait_for_finality,
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

    def __init__(self, *, address: str) -> None:
        self.address = address


class _FakeChangedObject:
    """Stand-in for ``sui_prot.ChangedObject``."""

    def __init__(
        self,
        *,
        object_id: str | None,
        id_operation: object,
        output_owner: _FakeOwner | None = None,
    ) -> None:
        self.object_id = object_id
        self.id_operation = id_operation
        self.output_owner = output_owner


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
    """Field extraction from a freshly created Blob object's JSON view."""

    def test_happy_path(self) -> None:
        """end_epoch and deletable are read from the nested storage/deletable fields."""
        end_epoch, deletable = _end_epoch_and_deletable(_blob_object())  # type: ignore[arg-type]
        assert end_epoch == 42
        assert deletable is True

    def test_deletable_false(self) -> None:
        """A permanent (non-deletable) blob's flag round-trips as False."""
        _, deletable = _end_epoch_and_deletable(  # type: ignore[arg-type]
            _blob_object(deletable=False)
        )
        assert deletable is False

    def test_no_json_view_raises(self) -> None:
        """A response with no JSON view raises ValueError rather than being
        silently treated as ineligible."""
        obj = _FakeObject(object_id="0xblob", json=None)
        with pytest.raises(ValueError):
            _end_epoch_and_deletable(obj)  # type: ignore[arg-type]

    def test_missing_storage_field_raises(self) -> None:
        """A missing 'storage' field raises ValueError."""
        obj = _FakeObject(
            object_id="0xblob",
            json=_FakeJson(
                struct_value=_FakeStruct(fields={"deletable": _FakeValue(bool_value=True)})
            ),
        )
        with pytest.raises(ValueError):
            _end_epoch_and_deletable(obj)  # type: ignore[arg-type]

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
            _end_epoch_and_deletable(obj)  # type: ignore[arg-type]

    def test_missing_deletable_raises(self) -> None:
        """A missing 'deletable' field raises ValueError."""
        obj = _blob_object(deletable=None)
        with pytest.raises(ValueError):
            _end_epoch_and_deletable(obj)  # type: ignore[arg-type]


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
            _find_created_object_id(effects=effects, owner="0xsender")  # type: ignore[arg-type]
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
            _find_created_object_id(effects=effects, owner="0xsender")  # type: ignore[arg-type]

    def test_no_changed_objects_raises(self) -> None:
        """An empty changed_objects list raises RuntimeError."""
        with pytest.raises(RuntimeError):
            _find_created_object_id(effects=_FakeEffects(), owner="0xsender")  # type: ignore[arg-type]


class TestRequireSuccess:
    """Status checking on a submitted transaction's effects."""

    def test_success_returns_effects(self) -> None:
        """A successful status returns the effects object unchanged."""
        effects = _FakeEffects(status=_FakeStatus(success=True))
        result_data = _FakeResultData(effects=effects)
        assert (
            _require_success(result_data=result_data, label="test")  # type: ignore[arg-type]
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
            _require_success(result_data=result_data, label="test")  # type: ignore[arg-type]

    def test_failure_without_error_uses_unknown(self) -> None:
        """An abort with no error detail falls back to 'unknown error'."""
        effects = _FakeEffects(status=_FakeStatus(success=False, error=None))
        result_data = _FakeResultData(effects=effects)
        with pytest.raises(RuntimeError, match="unknown error"):
            _require_success(result_data=result_data, label="test")  # type: ignore[arg-type]

    def test_no_effects_raises(self) -> None:
        """A response carrying no effects at all raises RuntimeError."""
        result_data = _FakeResultData(effects=None)
        with pytest.raises(RuntimeError):
            _require_success(result_data=result_data, label="test")  # type: ignore[arg-type]


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
    ``pytusk.core.system_ops``, i.e. its plain ``import asyncio``) so these
    tests run fast and can assert exactly how many times -- and after which
    attempts -- a delay was taken.
    """

    def _patch_sleep(self, *, monkeypatch: pytest.MonkeyPatch) -> list[float]:
        """Replace ``system_ops.asyncio.sleep`` with a recorder and return
        the list it appends delays to."""
        sleep_calls: list[float] = []

        async def _fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        monkeypatch.setattr(system_ops.asyncio, "sleep", _fake_sleep)
        return sleep_calls

    async def test_visible_immediately(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A first poll that is already ok with non-None result_data returns
        True after exactly one call, with no sleep taken."""
        sleep_calls = self._patch_sleep(monkeypatch=monkeypatch)
        client = _FakeFinalityClient(responses=[SuiRpcResult(True, "", object())])

        visible = await wait_for_finality(client=client, digest="0xdigest")  # type: ignore[arg-type]

        assert visible is True
        assert len(client.calls) == 1
        assert sleep_calls == []

    async def test_visible_after_polls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two not-visible polls -- one not-ok, one ok with result_data still
        None (the real-world pre-checkpoint race) -- followed by a visible
        third poll returns True after exactly three calls."""
        sleep_calls = self._patch_sleep(monkeypatch=monkeypatch)
        client = _FakeFinalityClient(
            responses=[
                SuiRpcResult(False, "not found", None),
                SuiRpcResult(True, "", None),
                SuiRpcResult(True, "", object()),
            ]
        )

        visible = await wait_for_finality(client=client, digest="0xdigest")  # type: ignore[arg-type]

        assert visible is True
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
            system_ops, "ExecuteTransaction", lambda **kwargs: object()
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
            system_ops, "ExecuteTransaction", lambda **kwargs: object()
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
