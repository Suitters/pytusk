#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for ``pytusk.core.storage_ops``.

Four layers are covered, each for a different reason:

- The VALIDATORS (``fuse_incompatibility``,
  ``fuse_periods_incompatibility``, ``validate_fuse_pair``) are pure
  functions with no client at all, and they encode a Move contract's
  dispatch and assertion order. They are the highest-value tests in this
  file: every row of ``storage_resource::fuse``'s truth table is pinned
  here, including the deliberate CHECK ORDER (size before adjacency) that
  makes a client-side rejection name the same abort Move itself would
  have raised.
- The PARSERS (``storage_from_object``, ``storage_from_blob``) read a
  proto JSON view, so they are tested against fakes shaped like the
  betterproto messages -- notably the ``u64``-as-decimal-STRING encoding
  of ``storage_size``, which a naive ``number_value`` read would silently
  report as zero.
- The ``add_*`` layer is PURE PTB COMPOSITION (see that module's
  "caller owns the transaction lifecycle" note), so a recording fake
  standing in for ``AsyncSuiTransaction`` genuinely proves target strings
  and argument ORDER rather than faking coverage. Argument order is
  load-bearing: ``fuse``'s first argument survives and its second is
  consumed, so a transposition would delete the wrong object.
- The ``execute_*`` wrappers are thin, and their thinness is the thing
  worth pinning: which result dataclass comes back, that a split
  transfers its new ``Storage`` (an unconsumed object result would abort
  the PTB) while fuse/destroy transfer nothing, and that both the
  submission-failure and on-chain-abort paths raise ``RuntimeError``.

CLI handler tests are deliberately absent -- tusky is validated
interactively.
"""

import dataclasses

import pysui.sui.sui_grpc.suimsgs.sui.rpc.v2 as sui_prot
import pytest
from pysui import SuiRpcResult

from pytusk.config.tusk_config import NetworkType
from pytusk.core import storage_ops
from pytusk.core.storage_ops import (
    SplitResult,
    StorageObject,
    StorageOpResult,
    add_destroy_storage,
    add_fuse,
    add_split_by_epoch,
    add_split_by_size,
    execute_destroy_storage,
    execute_fuse,
    execute_split_by_epoch,
    execute_split_by_size,
    fuse_incompatibility,
    fuse_periods_incompatibility,
    list_storage_objects,
    storage_from_blob,
    storage_from_object,
    validate_fuse_pair,
)

_PACKAGE = "0xpkg"
_SENDER = "0xactive"


class _FakeValue:
    """Stand-in for a protobuf ``Value``, with every oneof member defaulted.

    Differs deliberately from the equivalent fake in ``test_system_ops.py``
    / ``test_move_field_parity.py``, which sets ONLY the fields passed and
    lets the rest be absent. ``_storage_from_field_map`` reaches for
    ``.string_value`` and ``.number_value`` by DIRECT attribute access
    rather than ``getattr(..., default)``, so an absent attribute would
    raise ``AttributeError`` here where real betterproto returns the
    field's zero value. Defaulting all four members matches the real
    message and keeps these tests exercising the parser's own branch
    logic instead of the fake's shape.
    """

    def __init__(self, **fields: object) -> None:
        self.string_value: object = ""
        self.number_value: object = 0.0
        self.bool_value: object = False
        self.struct_value: object = None
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
    """Stand-in for ``sui_prot.Object``.

    ``object_type`` defaults to "" -- absent for the many tests (parsers,
    ``add_*``, ``execute_*``) that never read it; only the non-production
    ``list_storage_objects`` path reads it, to filter by type SUFFIX
    client-side rather than by exact package address.
    """

    def __init__(
        self, *, object_id: str, json: _FakeJson | None, object_type: str = ""
    ) -> None:
        self.object_id = object_id
        self.json = json
        self.object_type = object_type


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


class _FakeStatus:
    """Stand-in for ``sui_prot.ExecutionStatus``."""

    def __init__(self, *, success: bool, error: object | None = None) -> None:
        self.success = success
        self.error = error


class _FakeExecutionError:
    """Stand-in for ``sui_prot.ExecutionError``."""

    def __init__(self, *, description: str) -> None:
        self.description = description


class _FakeEffects:
    """Stand-in for ``sui_prot.TransactionEffects``."""

    def __init__(
        self,
        *,
        status: _FakeStatus | None,
        changed_objects: list[_FakeChangedObject] | None = None,
    ) -> None:
        self.status = status
        self.changed_objects = changed_objects or []


class _FakeResultData:
    """Stand-in for the ``result_data`` an ``ExecuteTransaction`` carries.

    ``storage_ops`` reads exactly two attributes off it: ``.effects``
    (via ``require_success``) and ``.digest``.
    """

    def __init__(self, *, effects: _FakeEffects | None, digest: str) -> None:
        self.effects = effects
        self.digest = digest


class _FakeListResultData:
    """Stand-in for the ``result_data`` a ``GetObjectsForType`` carries."""

    def __init__(self, *, objects: list[_FakeObject]) -> None:
        self.objects = objects


class _RecordingTxn:
    """Minimal recording fake for ``AsyncSuiTransaction``.

    Captures every ``move_call``/``transfer_objects`` call, in order, in
    ``calls``. Each ``move_call`` returns a distinct sentinel string
    (``"result1"``, ``"result2"``, ...) recorded alongside the call under
    the ``"result"`` key, so a later ``transfer_objects`` argument can be
    asserted to be the SAME object a specific earlier ``move_call``
    produced -- not merely something that looks like it.
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
        """Return an empty txdict.

        Every test that reaches this monkeypatches ``ExecuteTransaction``
        to a stub accepting any kwargs, so the empty dict never has to
        satisfy the real command's signature.
        """
        return {}

    def move_calls(self) -> list[dict[str, object]]:
        """Return just the recorded ``move_call`` kwargs, in order."""
        return [kwargs for kind, kwargs in self.calls if kind == "move_call"]

    def transfers(self) -> list[dict[str, object]]:
        """Return just the recorded ``transfer_objects`` kwargs, in order."""
        return [kwargs for kind, kwargs in self.calls if kind == "transfer_objects"]


class _FakePysuiConfig:
    """Stand-in for ``WalrusClient.pysui_client.config``.

    Exposes only ``active_address`` -- the sole attribute the
    ``execute_*`` wrappers read from it, when resolving a ``None`` sender.
    """

    def __init__(self, *, active_address: str) -> None:
        self.active_address = active_address


class _FakePysuiClient:
    """Stand-in for ``WalrusClient.pysui_client``."""

    def __init__(self, *, active_address: str) -> None:
        self.config = _FakePysuiConfig(active_address=active_address)


class _FakeClient:
    """Minimal ``WalrusClient``-shaped fake for the ``execute_*`` wrappers.

    ``.transaction()`` returns the supplied recording ``txn`` and records
    the sender/sponsor it was opened with. ``.execute()`` returns the
    single canned ``SuiRpcResult`` and records the dispatched command.

    Note what is ABSENT: no ``GetObject``, no finality polling hook. The
    ``execute_*`` wrappers are specified to do no read-back, so any such
    call would fail here with ``AttributeError`` rather than passing
    silently -- the absence is the assertion.
    """

    def __init__(self, *, txn: _RecordingTxn, response: SuiRpcResult) -> None:
        self._txn = txn
        self._response = response
        self.pysui_client = _FakePysuiClient(active_address=_SENDER)
        self.transaction_calls: list[dict[str, object]] = []
        self.executed: list[object] = []

    async def transaction(
        self, *, initial_sender: str, initial_sponsor: str | None
    ) -> _RecordingTxn:
        """Record how the transaction was opened and return the fake."""
        self.transaction_calls.append(
            {"initial_sender": initial_sender, "initial_sponsor": initial_sponsor}
        )
        return self._txn

    async def execute(self, *, command: object) -> SuiRpcResult:
        """Record the dispatched command and return the canned response."""
        self.executed.append(command)
        return self._response


class _FakeNetworkConfig:
    """Stand-in for ``WalrusClient.config.network``.

    Exposes only ``network_type`` -- the sole attribute
    ``list_storage_objects`` reads from it to choose between the
    PRODUCTION (server-side type filter) and non-PRODUCTION (client-side
    suffix scan) listing paths.
    """

    def __init__(self, *, network_type: NetworkType) -> None:
        self.network_type = network_type


class _FakePytuskConfig:
    """Stand-in for ``WalrusClient.config``, exposing only ``.network``."""

    def __init__(self, *, network_type: NetworkType) -> None:
        self.network = _FakeNetworkConfig(network_type=network_type)


class _FakeListClient:
    """Minimal ``WalrusClient``-shaped fake for ``list_storage_objects``.

    Implements ``execute_for_all`` (recording each dispatched command for
    assertions) plus ``.config.network.network_type``, the network signal
    the function branches on. Defaults to ``NetworkType.TEST`` -- the
    non-PRODUCTION, suffix-scan path this module's live testnet defect was
    found against -- so tests exercising the PRODUCTION path must pass
    ``network_type`` explicitly.
    """

    def __init__(
        self,
        *,
        response: SuiRpcResult,
        network_type: NetworkType = NetworkType.TEST,
    ) -> None:
        self._response = response
        self.commands: list[object] = []
        self.config = _FakePytuskConfig(network_type=network_type)

    async def execute_for_all(self, *, command: object) -> SuiRpcResult:
        """Record the dispatched command and return the canned response."""
        self.commands.append(command)
        return self._response


class _RecordingCommand:
    """Recorder standing in for ``GetObjectsForType``.

    Monkeypatched over the real command so the TYPE FILTER STRING
    ``storage_ops`` builds can be asserted directly. Asserting on the real
    command would test betterproto's field names rather than the string
    under test.
    """

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


def _value_number(value: float) -> _FakeValue:
    """A proto ``Value`` carrying a JSON number, as ``u32`` fields arrive."""
    return _FakeValue(number_value=value)


def _value_string(value: str) -> _FakeValue:
    """A proto ``Value`` carrying a JSON string, as ``u64`` fields arrive."""
    return _FakeValue(string_value=value)


def _storage_fields(
    *,
    start_epoch: float | None = 1.0,
    end_epoch: float | None = 5.0,
    storage_size: _FakeValue | None = None,
) -> dict[str, object]:
    """Build a ``Storage`` struct field map.

    Any field passed ``None`` is OMITTED entirely, which is how a missing
    field arrives from the node -- distinct from a field present with a
    zero value.
    """
    fields: dict[str, object] = {}
    if start_epoch is not None:
        fields["start_epoch"] = _value_number(start_epoch)
    if end_epoch is not None:
        fields["end_epoch"] = _value_number(end_epoch)
    if storage_size is not None:
        fields["storage_size"] = storage_size
    return fields


def _storage_object(
    *,
    object_id: str = "0xstorage",
    start_epoch: float | None = 1.0,
    end_epoch: float | None = 5.0,
    storage_size: _FakeValue | None = None,
    object_type: str = f"{_PACKAGE}::storage_resource::Storage",
) -> _FakeObject:
    """Build a fake standalone ``Storage`` object."""
    return _FakeObject(
        object_id=object_id,
        object_type=object_type,
        json=_FakeJson(
            struct_value=_FakeStruct(
                fields=_storage_fields(
                    start_epoch=start_epoch,
                    end_epoch=end_epoch,
                    storage_size=storage_size
                    if storage_size is not None
                    else _value_string("1048576"),
                )
            )
        ),
    )


def _blob_object(
    *,
    object_id: str = "0xblob",
    storage: object | None = None,
    include_storage: bool = True,
    object_type: str = f"{_PACKAGE}::blob::Blob",
) -> _FakeObject:
    """Build a fake ``Blob`` object with a ``Storage`` embedded by value."""
    fields: dict[str, object] = {"blob_id": _value_string("abc")}
    if include_storage:
        fields["storage"] = (
            storage
            if storage is not None
            else _FakeValue(
                struct_value=_FakeStruct(
                    fields=_storage_fields(storage_size=_value_string("2048"))
                )
            )
        )
    return _FakeObject(
        object_id=object_id,
        object_type=object_type,
        json=_FakeJson(struct_value=_FakeStruct(fields=fields)),
    )


def _created(*, object_id: str, owner: str) -> _FakeChangedObject:
    """A ``ChangedObject`` recording an object CREATED and owned by ``owner``."""
    return _FakeChangedObject(
        object_id=object_id,
        id_operation=sui_prot.ChangedObjectIdOperation.CREATED,
        output_owner=_FakeOwner(address=owner),
    )


def _mutated(*, object_id: str, owner: str) -> _FakeChangedObject:
    """A ``ChangedObject`` that is NOT a creation, so must be skipped."""
    return _FakeChangedObject(
        object_id=object_id,
        id_operation=sui_prot.ChangedObjectIdOperation.NONE,
        output_owner=_FakeOwner(address=owner),
    )


def _ok_result(
    *,
    digest: str = "0xdigest",
    changed_objects: list[_FakeChangedObject] | None = None,
) -> SuiRpcResult:
    """A submitted-and-succeeded ``ExecuteTransaction`` result."""
    return SuiRpcResult(
        True,
        "",
        _FakeResultData(
            effects=_FakeEffects(
                status=_FakeStatus(success=True),
                changed_objects=changed_objects,
            ),
            digest=digest,
        ),
    )


def _aborted_result(*, description: str = "EIncompatibleEpochs") -> SuiRpcResult:
    """A SUBMITTED result whose transaction aborted on-chain.

    ``is_ok()`` is True -- submission succeeded -- and the failure is
    carried in ``effects.status``, which is the case ``require_success``
    exists to catch.
    """
    return SuiRpcResult(
        True,
        "",
        _FakeResultData(
            effects=_FakeEffects(
                status=_FakeStatus(
                    success=False,
                    error=_FakeExecutionError(description=description),
                )
            ),
            digest="0xdigest",
        ),
    )


def _submission_failure(*, message: str = "node refused") -> SuiRpcResult:
    """A result whose SUBMISSION failed, before any on-chain execution."""
    return SuiRpcResult(False, message)


def _stub_execute_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize ``ExecuteTransaction`` construction from an empty txdict."""
    monkeypatch.setattr(storage_ops, "ExecuteTransaction", lambda **kwargs: object())


def _storage(
    *,
    object_id: str = "",
    start_epoch: int,
    end_epoch: int,
    storage_size: int = 100,
) -> StorageObject:
    """Build a :class:`StorageObject` for the validator truth table."""
    return StorageObject(
        object_id=object_id,
        start_epoch=start_epoch,
        end_epoch=end_epoch,
        storage_size=storage_size,
    )


class TestFuseIncompatibilityTruthTable:
    """Every row of ``storage_resource::fuse``'s dispatch truth table.

    ``fuse`` routes purely on ``first.start_epoch == second.start_epoch``:
    equal goes to ``fuse_amount`` (identical range required, sizes
    summed), unequal goes to ``fuse_periods`` (equal sizes required,
    ranges joined). Each row below pins one route/outcome pair.
    """

    def test_row1_same_range_fuses_by_amount(self) -> None:
        """Same start AND same end: fuse_amount succeeds, sizes may differ."""
        first = _storage(start_epoch=1, end_epoch=5, storage_size=100)
        second = _storage(start_epoch=1, end_epoch=5, storage_size=200)
        assert fuse_incompatibility(first=first, second=second) is None

    def test_row2_same_start_different_end_rejected(self) -> None:
        """Same start, different end: fuse_amount needs an identical range."""
        first = _storage(start_epoch=1, end_epoch=5, storage_size=100)
        second = _storage(start_epoch=1, end_epoch=9, storage_size=100)
        reason = fuse_incompatibility(first=first, second=second)
        assert reason is not None
        assert "EIncompatibleEpochs" in reason
        assert "identical epoch range" in reason

    def test_row3_adjacent_equal_sizes_fuses_by_period(self) -> None:
        """Different start, equal sizes, adjacent: fuse_periods succeeds."""
        first = _storage(start_epoch=1, end_epoch=5, storage_size=100)
        second = _storage(start_epoch=5, end_epoch=9, storage_size=100)
        assert fuse_incompatibility(first=first, second=second) is None

    def test_row3_adjacency_works_in_either_direction(self) -> None:
        """``first.start_epoch == second.end_epoch`` is adjacency too.

        Fusion is not ordered in time: the second storage may sit BEFORE
        the first as readily as after it.
        """
        first = _storage(start_epoch=5, end_epoch=9, storage_size=100)
        second = _storage(start_epoch=1, end_epoch=5, storage_size=100)
        assert fuse_incompatibility(first=first, second=second) is None

    def test_row4_non_adjacent_rejected(self) -> None:
        """Different start, equal sizes, a GAP between the ranges."""
        first = _storage(start_epoch=1, end_epoch=5, storage_size=100)
        second = _storage(start_epoch=7, end_epoch=9, storage_size=100)
        reason = fuse_incompatibility(first=first, second=second)
        assert reason is not None
        assert "not adjacent" in reason
        assert "EIncompatibleEpochs" in reason

    def test_row5_size_is_checked_before_adjacency(self) -> None:
        """A pair failing BOTH period checks reports the SIZE one.

        Move asserts size before adjacency, so a caller pre-flighting
        client-side must be told the same thing the chain would have told
        them. This pair is both mismatched in size and non-adjacent; the
        reported abort must be ``EIncompatibleAmount``.
        """
        first = _storage(start_epoch=1, end_epoch=5, storage_size=100)
        second = _storage(start_epoch=7, end_epoch=9, storage_size=200)
        reason = fuse_incompatibility(first=first, second=second)
        assert reason is not None
        assert "EIncompatibleAmount" in reason
        assert "EIncompatibleEpochs" not in reason
        assert "not adjacent" not in reason


class TestFuseIncompatibilitySelfFuse:
    """The guard rejecting one storage passed as both operands."""

    def test_same_object_id_rejected(self) -> None:
        """Fusing a storage with itself needs two distinct objects."""
        first = _storage(object_id="0xsame", start_epoch=1, end_epoch=5)
        second = _storage(object_id="0xsame", start_epoch=1, end_epoch=5)
        reason = fuse_incompatibility(first=first, second=second)
        assert reason is not None
        assert "with itself" in reason
        assert "0xsame" in reason

    def test_self_fuse_guard_precedes_range_checks(self) -> None:
        """The self-fuse reason wins even when the ranges also disagree."""
        first = _storage(object_id="0xsame", start_epoch=1, end_epoch=5)
        second = _storage(object_id="0xsame", start_epoch=1, end_epoch=9)
        reason = fuse_incompatibility(first=first, second=second)
        assert reason is not None
        assert "with itself" in reason

    def test_two_wrapped_storages_are_not_self_fuse(self) -> None:
        """Empty object_ids must not collide into a false self-fuse.

        A ``StorageObject`` describing storage WRAPPED in a blob carries
        ``object_id == ""``. Two such values are not the same object, and
        a bare equality check would wrongly reject them.
        """
        first = _storage(object_id="", start_epoch=1, end_epoch=5)
        second = _storage(object_id="", start_epoch=1, end_epoch=5)
        assert fuse_incompatibility(first=first, second=second) is None


class TestFusePeriodsIncompatibility:
    """``fuse_periods`` judged directly, bypassing ``fuse``'s dispatch."""

    def test_equal_size_and_adjacent_passes(self) -> None:
        """The success case: equal sizes, ranges meeting exactly."""
        first = _storage(start_epoch=1, end_epoch=5, storage_size=100)
        second = _storage(start_epoch=5, end_epoch=9, storage_size=100)
        assert fuse_periods_incompatibility(first=first, second=second) is None

    def test_unequal_sizes_rejected(self) -> None:
        """Periods can only be joined between equally sized reservations."""
        first = _storage(start_epoch=1, end_epoch=5, storage_size=100)
        second = _storage(start_epoch=5, end_epoch=9, storage_size=200)
        reason = fuse_periods_incompatibility(first=first, second=second)
        assert reason is not None
        assert "EIncompatibleAmount" in reason

    def test_size_checked_before_adjacency(self) -> None:
        """Assertion order is preserved here too, not just in ``fuse``."""
        first = _storage(start_epoch=1, end_epoch=5, storage_size=100)
        second = _storage(start_epoch=7, end_epoch=9, storage_size=200)
        reason = fuse_periods_incompatibility(first=first, second=second)
        assert reason is not None
        assert "EIncompatibleAmount" in reason
        assert "not adjacent" not in reason

    def test_diverges_from_fuse_incompatibility_on_shared_start_epoch(
        self,
    ) -> None:
        """The two validators DISAGREE on a shared-start_epoch pair.

        ``blob::extend_with_resource`` calls ``fuse_periods`` directly,
        bypassing ``fuse``'s start_epoch dispatch. So on a pair sharing a
        start_epoch, ``fuse_incompatibility`` reports the fuse_amount
        rule while ``fuse_periods_incompatibility`` reports the period
        rule -- and only the latter is what the extend path enforces.
        This divergence is intended; pinning it stops a future
        "consolidation" from collapsing the two and silently reporting
        the wrong abort on the extend path.
        """
        first = _storage(start_epoch=1, end_epoch=5, storage_size=100)
        second = _storage(start_epoch=1, end_epoch=9, storage_size=100)

        amount_reason = fuse_incompatibility(first=first, second=second)
        period_reason = fuse_periods_incompatibility(first=first, second=second)

        assert amount_reason is not None
        assert period_reason is not None
        assert amount_reason != period_reason
        assert "identical epoch range" in amount_reason
        assert "not adjacent" in period_reason


class TestValidateFusePair:
    """The raising wrapper over ``fuse_incompatibility``."""

    def test_compatible_pair_returns_none(self) -> None:
        """A fusable pair passes silently."""
        first = _storage(start_epoch=1, end_epoch=5, storage_size=100)
        second = _storage(start_epoch=5, end_epoch=9, storage_size=100)
        validate_fuse_pair(first=first, second=second)  # no raise == compatible

    def test_incompatible_pair_raises_value_error(self) -> None:
        """``ValueError``, per the project's no-custom-exceptions norm."""
        first = _storage(start_epoch=1, end_epoch=5, storage_size=100)
        second = _storage(start_epoch=7, end_epoch=9, storage_size=100)
        with pytest.raises(ValueError, match="EIncompatibleEpochs"):
            validate_fuse_pair(first=first, second=second)

    def test_raised_message_is_the_validator_reason_verbatim(self) -> None:
        """The wrapper must not reword what the validator reported."""
        first = _storage(start_epoch=1, end_epoch=5, storage_size=100)
        second = _storage(start_epoch=7, end_epoch=9, storage_size=200)
        reason = fuse_incompatibility(first=first, second=second)
        with pytest.raises(ValueError) as excinfo:
            validate_fuse_pair(first=first, second=second)
        assert str(excinfo.value) == reason

    def test_self_fuse_raises(self) -> None:
        """The self-fuse guard reaches the raising path too."""
        first = _storage(object_id="0xsame", start_epoch=1, end_epoch=5)
        second = _storage(object_id="0xsame", start_epoch=1, end_epoch=5)
        with pytest.raises(ValueError, match="with itself"):
            validate_fuse_pair(first=first, second=second)


class TestStorageFromObject:
    """Parsing a standalone ``Storage`` object's JSON view."""

    def test_parses_all_fields(self) -> None:
        """Epochs come from numbers, size from a decimal string."""
        obj = _storage_object(
            object_id="0xstorage",
            start_epoch=3.0,
            end_epoch=11.0,
            storage_size=_value_string("1048576"),
        )
        parsed = storage_from_object(obj=obj)  # type: ignore[arg-type]
        assert parsed == StorageObject(
            object_id="0xstorage",
            start_epoch=3,
            end_epoch=11,
            storage_size=1048576,
        )

    def test_u64_size_as_string_is_not_read_as_zero(self) -> None:
        """A ``u64`` arrives as a STRING; reading ``number_value`` gives 0.

        This is the failure this branch exists to prevent -- every
        reservation silently reported as zero bytes -- so it is pinned
        with a value far past the point where the string form matters.
        """
        obj = _storage_object(storage_size=_value_string("18446744073709551615"))
        parsed = storage_from_object(obj=obj)  # type: ignore[arg-type]
        assert parsed.storage_size == 18446744073709551615

    def test_numeric_size_is_also_accepted(self) -> None:
        """A number-shaped size still parses, so a proto change degrades
        to a loud wrong parse rather than a silent zero."""
        obj = _storage_object(storage_size=_value_number(4096.0))
        parsed = storage_from_object(obj=obj)  # type: ignore[arg-type]
        assert parsed.storage_size == 4096

    def test_missing_size_field_defaults_to_zero(self) -> None:
        """An absent ``storage_size`` is 0, not an error."""
        obj = _FakeObject(
            object_id="0xstorage",
            json=_FakeJson(
                struct_value=_FakeStruct(fields=_storage_fields(storage_size=None))
            ),
        )
        parsed = storage_from_object(obj=obj)  # type: ignore[arg-type]
        assert parsed.storage_size == 0

    def test_missing_epoch_fields_default_to_zero(self) -> None:
        """Absent epoch fields are 0, not an error."""
        obj = _FakeObject(
            object_id="0xstorage",
            json=_FakeJson(
                struct_value=_FakeStruct(
                    fields=_storage_fields(
                        start_epoch=None,
                        end_epoch=None,
                        storage_size=_value_string("64"),
                    )
                )
            ),
        )
        parsed = storage_from_object(obj=obj)  # type: ignore[arg-type]
        assert parsed.start_epoch == 0
        assert parsed.end_epoch == 0

    def test_no_json_view_raises(self) -> None:
        """A single object fetched by ID must RAISE when unreadable."""
        obj = _FakeObject(object_id="0xstorage", json=None)
        with pytest.raises(ValueError, match="no JSON view"):
            storage_from_object(obj=obj)  # type: ignore[arg-type]

    def test_no_struct_value_raises(self) -> None:
        """A JSON view present but carrying no struct is equally unreadable."""
        obj = _FakeObject(object_id="0xstorage", json=_FakeJson(struct_value=None))
        with pytest.raises(ValueError, match="no JSON view"):
            storage_from_object(obj=obj)  # type: ignore[arg-type]

    def test_unknown_object_id_is_named_in_the_error(self) -> None:
        """An empty object_id degrades to ``(unknown)`` rather than blank."""
        obj = _FakeObject(object_id="", json=None)
        with pytest.raises(ValueError, match=r"\(unknown\)"):
            storage_from_object(obj=obj)  # type: ignore[arg-type]


class TestStorageFromBlob:
    """Reading the ``Storage`` embedded by value inside a ``Blob``."""

    def test_parses_embedded_storage(self) -> None:
        """Fields are read from the nested ``storage`` struct."""
        obj = _blob_object()
        parsed = storage_from_blob(obj=obj)  # type: ignore[arg-type]
        assert parsed.start_epoch == 1
        assert parsed.end_epoch == 5
        assert parsed.storage_size == 2048

    def test_object_id_is_always_empty(self) -> None:
        """Wrapped storage has no independently addressable ID.

        The blob's OWN id must not leak into the field, or a caller could
        try to fetch or destroy an object that does not resolve.
        """
        obj = _blob_object(object_id="0xblob")
        parsed = storage_from_blob(obj=obj)  # type: ignore[arg-type]
        assert parsed.object_id == ""

    def test_no_json_view_raises(self) -> None:
        """An unreadable blob raises rather than reporting zeros."""
        obj = _FakeObject(object_id="0xblob", json=None)
        with pytest.raises(ValueError, match="no JSON view"):
            storage_from_blob(obj=obj)  # type: ignore[arg-type]

    def test_missing_storage_field_raises(self) -> None:
        """An object with no ``storage`` field may not be a Blob at all."""
        obj = _blob_object(include_storage=False)
        with pytest.raises(ValueError, match="no 'storage' field"):
            storage_from_blob(obj=obj)  # type: ignore[arg-type]

    def test_storage_field_without_struct_raises(self) -> None:
        """A ``storage`` field carrying no struct is equally unusable."""
        obj = _blob_object(storage=_FakeValue(string_value="not-a-struct"))
        with pytest.raises(ValueError, match="no 'storage' field"):
            storage_from_blob(obj=obj)  # type: ignore[arg-type]


class TestAddSplitByEpoch:
    """PTB composition for ``storage_resource::split_by_epoch``."""

    async def test_composes_single_move_call(self) -> None:
        """One move_call, correct target, argument order, and no generics."""
        txn = _RecordingTxn()
        await add_split_by_epoch(
            txn=txn,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            storage_object_id="0xstorage",
            split_epoch=7,
        )
        calls = txn.move_calls()
        assert len(calls) == 1
        assert calls[0]["target"] == "0xpkg::storage_resource::split_by_epoch"
        assert calls[0]["arguments"] == ["0xstorage", 7]
        assert calls[0]["type_arguments"] == []

    async def test_returns_the_move_call_result(self) -> None:
        """The new ``Storage`` command result is handed back to the caller."""
        txn = _RecordingTxn()
        result = await add_split_by_epoch(
            txn=txn,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            storage_object_id="0xstorage",
            split_epoch=7,
        )
        assert result == txn.move_calls()[0]["result"]

    async def test_does_not_transfer_the_new_storage(self) -> None:
        """The ``add_*`` layer leaves consumption to the caller.

        Deliberate: the caller decides where the new ``Storage`` goes.
        A transfer added here would silently take that choice away.
        """
        txn = _RecordingTxn()
        await add_split_by_epoch(
            txn=txn,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            storage_object_id="0xstorage",
            split_epoch=7,
        )
        assert txn.transfers() == []


class TestAddSplitBySize:
    """PTB composition for ``storage_resource::split_by_size``."""

    async def test_composes_single_move_call(self) -> None:
        """One move_call, correct target, argument order, and no generics."""
        txn = _RecordingTxn()
        await add_split_by_size(
            txn=txn,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            storage_object_id="0xstorage",
            split_size=4096,
        )
        calls = txn.move_calls()
        assert len(calls) == 1
        assert calls[0]["target"] == "0xpkg::storage_resource::split_by_size"
        assert calls[0]["arguments"] == ["0xstorage", 4096]
        assert calls[0]["type_arguments"] == []

    async def test_returns_the_move_call_result(self) -> None:
        """The remainder ``Storage`` command result is handed back."""
        txn = _RecordingTxn()
        result = await add_split_by_size(
            txn=txn,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            storage_object_id="0xstorage",
            split_size=4096,
        )
        assert result == txn.move_calls()[0]["result"]

    async def test_does_not_transfer_the_new_storage(self) -> None:
        """Consumption is the caller's decision here too."""
        txn = _RecordingTxn()
        await add_split_by_size(
            txn=txn,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            storage_object_id="0xstorage",
            split_size=4096,
        )
        assert txn.transfers() == []


class TestAddFuse:
    """PTB composition for ``storage_resource::fuse``."""

    async def test_composes_single_move_call(self) -> None:
        """One move_call, correct target, and no generics."""
        txn = _RecordingTxn()
        await add_fuse(
            txn=txn,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            first_storage_id="0xfirst",
            second_storage_id="0xsecond",
        )
        calls = txn.move_calls()
        assert len(calls) == 1
        assert calls[0]["target"] == "0xpkg::storage_resource::fuse"
        assert calls[0]["type_arguments"] == []

    async def test_argument_order_survivor_first(self) -> None:
        """Order is load-bearing: arg 0 SURVIVES, arg 1 is DELETED.

        Move takes ``first`` as ``&mut Storage`` and ``second`` by value.
        A transposition here would destroy the wrong storage object and
        the transaction would still succeed, so nothing downstream would
        catch it.
        """
        txn = _RecordingTxn()
        await add_fuse(
            txn=txn,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            first_storage_id="0xsurvivor",
            second_storage_id="0xconsumed",
        )
        assert txn.move_calls()[0]["arguments"] == ["0xsurvivor", "0xconsumed"]

    async def test_returns_none(self) -> None:
        """``fuse`` has no Move return value, so there is nothing to consume."""
        txn = _RecordingTxn()
        await add_fuse(
            txn=txn,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            first_storage_id="0xfirst",
            second_storage_id="0xsecond",
        )
        assert txn.transfers() == []


class TestAddDestroyStorage:
    """PTB composition for ``storage_resource::destroy``."""

    async def test_composes_single_move_call(self) -> None:
        """One move_call taking the storage by value, and no generics."""
        txn = _RecordingTxn()
        await add_destroy_storage(
            txn=txn,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            storage_object_id="0xstorage",
        )
        calls = txn.move_calls()
        assert len(calls) == 1
        assert calls[0]["target"] == "0xpkg::storage_resource::destroy"
        assert calls[0]["arguments"] == ["0xstorage"]
        assert calls[0]["type_arguments"] == []

    async def test_returns_none(self) -> None:
        """``destroy`` has no Move return value."""
        txn = _RecordingTxn()
        await add_destroy_storage(
            txn=txn,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            storage_object_id="0xstorage",
        )
        assert txn.transfers() == []


class TestExecuteSplitByEpoch:
    """The thin ``split_by_epoch`` wrapper."""

    async def test_success_returns_split_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both storage IDs and the digest come back in a ``SplitResult``."""
        _stub_execute_transaction(monkeypatch)
        txn = _RecordingTxn()
        client = _FakeClient(
            txn=txn,
            response=_ok_result(
                digest="0xsplitdigest",
                changed_objects=[_created(object_id="0xnew", owner=_SENDER)],
            ),
        )

        result = await execute_split_by_epoch(
            client=client,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            storage_object_id="0xstorage",
            split_epoch=7,
        )

        assert result == SplitResult(
            object_id="0xstorage",
            new_object_id="0xnew",
            digest="0xsplitdigest",
        )

    async def test_transfers_the_new_storage_to_resolved_sender(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The unconsumed result MUST be transferred or the PTB aborts.

        The transfer must also carry the exact command result the
        ``add_*`` call produced, not a look-alike.
        """
        _stub_execute_transaction(monkeypatch)
        txn = _RecordingTxn()
        client = _FakeClient(
            txn=txn,
            response=_ok_result(
                changed_objects=[_created(object_id="0xnew", owner=_SENDER)]
            ),
        )

        await execute_split_by_epoch(
            client=client,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            storage_object_id="0xstorage",
            split_epoch=7,
        )

        transfers = txn.transfers()
        assert len(transfers) == 1
        assert transfers[0]["recipient"] == _SENDER
        assert transfers[0]["transfers"] == [txn.move_calls()[0]["result"]]

    async def test_none_sender_resolves_to_active_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An omitted sender falls back to the config's active address."""
        _stub_execute_transaction(monkeypatch)
        txn = _RecordingTxn()
        client = _FakeClient(
            txn=txn,
            response=_ok_result(
                changed_objects=[_created(object_id="0xnew", owner=_SENDER)]
            ),
        )

        await execute_split_by_epoch(
            client=client,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            storage_object_id="0xstorage",
            split_epoch=7,
        )

        assert client.transaction_calls[0]["initial_sender"] == _SENDER
        assert client.transaction_calls[0]["initial_sponsor"] is None

    async def test_explicit_recipient_receives_the_new_storage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A given recipient is used for BOTH transfer and effects lookup.

        If the two ever disagreed, the transfer would succeed and the
        created-object lookup would then fail to find anything.
        """
        _stub_execute_transaction(monkeypatch)
        txn = _RecordingTxn()
        client = _FakeClient(
            txn=txn,
            response=_ok_result(
                changed_objects=[
                    _created(object_id="0xother", owner="0xstranger"),
                    _created(object_id="0xnew", owner="0xrecipient"),
                ]
            ),
        )

        result = await execute_split_by_epoch(
            client=client,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            storage_object_id="0xstorage",
            split_epoch=7,
            sender="0xexplicit",
            recipient="0xrecipient",
        )

        assert txn.transfers()[0]["recipient"] == "0xrecipient"
        assert result.new_object_id == "0xnew"

    async def test_sponsor_is_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A sponsor reaches the transaction it was meant to sponsor."""
        _stub_execute_transaction(monkeypatch)
        txn = _RecordingTxn()
        client = _FakeClient(
            txn=txn,
            response=_ok_result(
                changed_objects=[_created(object_id="0xnew", owner=_SENDER)]
            ),
        )

        await execute_split_by_epoch(
            client=client,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            storage_object_id="0xstorage",
            split_epoch=7,
            sponsor="0xsponsor",
        )

        assert client.transaction_calls[0]["initial_sponsor"] == "0xsponsor"

    async def test_submission_failure_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rejected submission surfaces as ``RuntimeError``."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(
            txn=_RecordingTxn(),
            response=_submission_failure(message="node refused"),
        )
        with pytest.raises(RuntimeError, match="split_by_epoch transaction failed"):
            await execute_split_by_epoch(
                client=client,  # type: ignore[arg-type]
                package_id=_PACKAGE,
                storage_object_id="0xstorage",
                split_epoch=7,
            )

    async def test_onchain_abort_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An accepted-but-aborted transaction is NOT reported as success."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(
            txn=_RecordingTxn(),
            response=_aborted_result(description="EInvalidEpoch"),
        )
        with pytest.raises(RuntimeError, match="aborted on-chain"):
            await execute_split_by_epoch(
                client=client,  # type: ignore[arg-type]
                package_id=_PACKAGE,
                storage_object_id="0xstorage",
                split_epoch=7,
            )

    async def test_missing_created_object_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A succeeded split with no locatable new object fails loudly."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(
            txn=_RecordingTxn(),
            response=_ok_result(
                changed_objects=[_mutated(object_id="0xstorage", owner=_SENDER)]
            ),
        )
        with pytest.raises(RuntimeError, match="newly created object"):
            await execute_split_by_epoch(
                client=client,  # type: ignore[arg-type]
                package_id=_PACKAGE,
                storage_object_id="0xstorage",
                split_epoch=7,
            )


class TestExecuteSplitBySize:
    """The thin ``split_by_size`` wrapper."""

    async def test_success_returns_split_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Composition delegates to ``add_split_by_size`` and reports both IDs."""
        _stub_execute_transaction(monkeypatch)
        txn = _RecordingTxn()
        client = _FakeClient(
            txn=txn,
            response=_ok_result(
                digest="0xsizedigest",
                changed_objects=[_created(object_id="0xremainder", owner=_SENDER)],
            ),
        )

        result = await execute_split_by_size(
            client=client,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            storage_object_id="0xstorage",
            split_size=4096,
        )

        assert result == SplitResult(
            object_id="0xstorage",
            new_object_id="0xremainder",
            digest="0xsizedigest",
        )
        assert txn.move_calls()[0]["target"] == "0xpkg::storage_resource::split_by_size"

    async def test_transfers_the_remainder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The remainder storage is consumed by a transfer."""
        _stub_execute_transaction(monkeypatch)
        txn = _RecordingTxn()
        client = _FakeClient(
            txn=txn,
            response=_ok_result(
                changed_objects=[_created(object_id="0xremainder", owner=_SENDER)]
            ),
        )

        await execute_split_by_size(
            client=client,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            storage_object_id="0xstorage",
            split_size=4096,
        )

        transfers = txn.transfers()
        assert len(transfers) == 1
        assert transfers[0]["transfers"] == [txn.move_calls()[0]["result"]]

    async def test_submission_failure_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The error names the operation that failed."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(txn=_RecordingTxn(), response=_submission_failure())
        with pytest.raises(RuntimeError, match="split_by_size transaction failed"):
            await execute_split_by_size(
                client=client,  # type: ignore[arg-type]
                package_id=_PACKAGE,
                storage_object_id="0xstorage",
                split_size=4096,
            )

    async def test_onchain_abort_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``EIncompatibleAmount`` from an oversized split reaches the caller."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(
            txn=_RecordingTxn(),
            response=_aborted_result(description="EIncompatibleAmount"),
        )
        with pytest.raises(RuntimeError, match="EIncompatibleAmount"):
            await execute_split_by_size(
                client=client,  # type: ignore[arg-type]
                package_id=_PACKAGE,
                storage_object_id="0xstorage",
                split_size=999999,
            )


class TestExecuteFuse:
    """The thin ``fuse`` wrapper."""

    async def test_success_returns_surviving_storage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reported object_id is the SURVIVOR, not the consumed storage."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(
            txn=_RecordingTxn(), response=_ok_result(digest="0xfusedigest")
        )

        result = await execute_fuse(
            client=client,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            first_storage_id="0xsurvivor",
            second_storage_id="0xconsumed",
        )

        assert result == StorageOpResult(object_id="0xsurvivor", digest="0xfusedigest")

    async def test_transfers_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``fuse`` returns no value, so there is nothing to consume."""
        _stub_execute_transaction(monkeypatch)
        txn = _RecordingTxn()
        client = _FakeClient(txn=txn, response=_ok_result())

        await execute_fuse(
            client=client,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            first_storage_id="0xsurvivor",
            second_storage_id="0xconsumed",
        )

        assert txn.transfers() == []

    async def test_does_not_preflight_compatibility(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No client-side validation, by design.

        Pre-flighting would require fetching both objects, breaking the
        thin shape. An incompatible pair is therefore expected to reach
        the chain and abort there -- the ``fuse_storage`` CLI command
        does the pre-flight instead. The fake client has no object-read
        method at all, so any fetch attempt would raise
        ``AttributeError`` rather than pass unnoticed.
        """
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(
            txn=_RecordingTxn(),
            response=_aborted_result(description="EIncompatibleEpochs"),
        )

        with pytest.raises(RuntimeError, match="aborted on-chain"):
            await execute_fuse(
                client=client,  # type: ignore[arg-type]
                package_id=_PACKAGE,
                first_storage_id="0xfirst",
                second_storage_id="0xsecond",
            )

        assert len(client.executed) == 1

    async def test_submission_failure_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rejected submission surfaces as ``RuntimeError``."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(txn=_RecordingTxn(), response=_submission_failure())
        with pytest.raises(RuntimeError, match="fuse transaction failed"):
            await execute_fuse(
                client=client,  # type: ignore[arg-type]
                package_id=_PACKAGE,
                first_storage_id="0xfirst",
                second_storage_id="0xsecond",
            )


class TestExecuteDestroyStorage:
    """The thin ``destroy`` wrapper."""

    async def test_success_returns_destroyed_storage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The destroyed ID is reported even though it no longer resolves."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(
            txn=_RecordingTxn(), response=_ok_result(digest="0xdestroydigest")
        )

        result = await execute_destroy_storage(
            client=client,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            storage_object_id="0xstorage",
        )

        assert result == StorageOpResult(
            object_id="0xstorage", digest="0xdestroydigest"
        )

    async def test_composes_destroy_and_transfers_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One destroy move_call; the storage is consumed by Move itself."""
        _stub_execute_transaction(monkeypatch)
        txn = _RecordingTxn()
        client = _FakeClient(txn=txn, response=_ok_result())

        await execute_destroy_storage(
            client=client,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            storage_object_id="0xstorage",
        )

        assert len(txn.move_calls()) == 1
        assert txn.move_calls()[0]["target"] == "0xpkg::storage_resource::destroy"
        assert txn.transfers() == []

    async def test_submission_failure_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rejected submission surfaces as ``RuntimeError``."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(txn=_RecordingTxn(), response=_submission_failure())
        with pytest.raises(RuntimeError, match="destroy transaction failed"):
            await execute_destroy_storage(
                client=client,  # type: ignore[arg-type]
                package_id=_PACKAGE,
                storage_object_id="0xstorage",
            )

    async def test_onchain_abort_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An abort is surfaced rather than reported as a destroyed object."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(
            txn=_RecordingTxn(), response=_aborted_result(description="boom")
        )
        with pytest.raises(RuntimeError, match="destroy transaction aborted"):
            await execute_destroy_storage(
                client=client,  # type: ignore[arg-type]
                package_id=_PACKAGE,
                storage_object_id="0xstorage",
            )


class TestListStorageObjectsProduction:
    """The owned-``Storage`` read helper on a PRODUCTION (mainnet) network.

    The package address is stable there, so the node does the filtering
    via ``GetObjectsForType``.
    """

    async def test_filters_by_storage_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The node filters by type; this pins the exact type string.

        A wrong type tag would return an empty list rather than an error,
        so this is the only place the string can be caught.
        """
        monkeypatch.setattr(storage_ops, "GetObjectsForType", _RecordingCommand)
        client = _FakeListClient(
            response=SuiRpcResult(True, "", _FakeListResultData(objects=[])),
            network_type=NetworkType.PRODUCTION,
        )

        await list_storage_objects(
            client=client,  # type: ignore[arg-type]
            owner="0xowner",
            package_id=_PACKAGE,
        )

        command = client.commands[0]
        assert isinstance(command, _RecordingCommand)
        assert command.kwargs["owner"] == "0xowner"
        assert command.kwargs["object_type"] == "0xpkg::storage_resource::Storage"

    async def test_parses_every_entry_in_node_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Results keep the node's ordering."""
        monkeypatch.setattr(storage_ops, "GetObjectsForType", _RecordingCommand)
        client = _FakeListClient(
            response=SuiRpcResult(
                True,
                "",
                _FakeListResultData(
                    objects=[
                        _storage_object(
                            object_id="0xfirst",
                            storage_size=_value_string("100"),
                        ),
                        _storage_object(
                            object_id="0xsecond",
                            storage_size=_value_string("200"),
                        ),
                    ]
                ),
            ),
            network_type=NetworkType.PRODUCTION,
        )

        storages = await list_storage_objects(
            client=client,  # type: ignore[arg-type]
            owner="0xowner",
            package_id=_PACKAGE,
        )

        assert [s.object_id for s in storages] == ["0xfirst", "0xsecond"]
        assert [s.storage_size for s in storages] == [100, 200]

    async def test_empty_listing_returns_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Owning no storage is not an error."""
        monkeypatch.setattr(storage_ops, "GetObjectsForType", _RecordingCommand)
        client = _FakeListClient(
            response=SuiRpcResult(True, "", _FakeListResultData(objects=[])),
            network_type=NetworkType.PRODUCTION,
        )

        assert (
            await list_storage_objects(
                client=client,  # type: ignore[arg-type]
                owner="0xowner",
                package_id=_PACKAGE,
            )
            == []
        )

    async def test_unreadable_entry_is_skipped_not_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The skip-vs-raise asymmetry, pinned on the SKIP side.

        A listing entry with no JSON view is dropped so one bad entry
        cannot fail the whole listing -- whereas ``storage_from_object``
        RAISES for the same input, because a caller who asked for ONE
        object by ID must be told the thing they asked for is unreadable.
        Both halves are asserted here so the pair cannot drift.
        """
        monkeypatch.setattr(storage_ops, "GetObjectsForType", _RecordingCommand)
        unreadable = _FakeObject(object_id="0xbroken", json=None)
        client = _FakeListClient(
            response=SuiRpcResult(
                True,
                "",
                _FakeListResultData(
                    objects=[
                        unreadable,
                        _storage_object(object_id="0xgood"),
                    ]
                ),
            ),
            network_type=NetworkType.PRODUCTION,
        )

        storages = await list_storage_objects(
            client=client,  # type: ignore[arg-type]
            owner="0xowner",
            package_id=_PACKAGE,
        )

        assert [s.object_id for s in storages] == ["0xgood"]
        with pytest.raises(ValueError):
            storage_from_object(obj=unreadable)  # type: ignore[arg-type]

    async def test_failed_listing_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failed query raises rather than reporting "owns nothing"."""
        monkeypatch.setattr(storage_ops, "GetObjectsForType", _RecordingCommand)
        client = _FakeListClient(
            response=SuiRpcResult(False, "node down"),
            network_type=NetworkType.PRODUCTION,
        )

        with pytest.raises(
            RuntimeError, match="Cannot list Storage objects for 0xowner"
        ):
            await list_storage_objects(
                client=client,  # type: ignore[arg-type]
                owner="0xowner",
                package_id=_PACKAGE,
            )


class TestListStorageObjectsNonProduction:
    """The owned-``Storage`` read helper on a non-PRODUCTION (testnet) network.

    A testnet Walrus package is periodically redeployed under a new
    address, and Move type tags never change once an object is minted --
    so this path scans EVERY owned object via ``GetObjectsOwnedByAddress``
    and filters client-side on the ``::storage_resource::Storage``
    SUFFIX, independent of which package address minted the object. This
    is the fix for a live-testnet defect: ``GetObjectsForType`` filtered
    by the CURRENT ``package_id`` found zero of nine real ``Storage``
    objects, all minted under a PRIOR package version.
    """

    async def test_uses_get_objects_owned_by_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No type filter is sent -- every owned object is fetched."""
        monkeypatch.setattr(storage_ops, "GetObjectsOwnedByAddress", _RecordingCommand)
        client = _FakeListClient(
            response=SuiRpcResult(True, "", _FakeListResultData(objects=[]))
        )

        await list_storage_objects(
            client=client,  # type: ignore[arg-type]
            owner="0xowner",
            package_id=_PACKAGE,
        )

        command = client.commands[0]
        assert isinstance(command, _RecordingCommand)
        assert command.kwargs["owner"] == "0xowner"
        assert "object_type" not in command.kwargs

    async def test_matches_storage_regardless_of_package_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact live defect this path fixes.

        Two ``Storage`` objects minted under DIFFERENT package addresses
        (simulating one minted before, one after, a testnet package
        redeploy) must BOTH be found. A ``GetObjectsForType`` filter on
        either single address would find at most one of them; the suffix
        filter finds both.
        """
        monkeypatch.setattr(storage_ops, "GetObjectsOwnedByAddress", _RecordingCommand)
        client = _FakeListClient(
            response=SuiRpcResult(
                True,
                "",
                _FakeListResultData(
                    objects=[
                        _storage_object(
                            object_id="0xold",
                            object_type="0xold_pkg::storage_resource::Storage",
                        ),
                        _storage_object(
                            object_id="0xnew",
                            object_type="0xnew_pkg::storage_resource::Storage",
                        ),
                    ]
                ),
            )
        )

        storages = await list_storage_objects(
            client=client,  # type: ignore[arg-type]
            owner="0xowner",
            package_id="0xnew_pkg",
        )

        assert {s.object_id for s in storages} == {"0xold", "0xnew"}

    async def test_non_storage_objects_are_excluded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``Blob`` (or any other type) in the owned-object list is dropped."""
        monkeypatch.setattr(storage_ops, "GetObjectsOwnedByAddress", _RecordingCommand)
        client = _FakeListClient(
            response=SuiRpcResult(
                True,
                "",
                _FakeListResultData(
                    objects=[
                        _blob_object(object_id="0xblob"),
                        _storage_object(object_id="0xstorage"),
                    ]
                ),
            )
        )

        storages = await list_storage_objects(
            client=client,  # type: ignore[arg-type]
            owner="0xowner",
            package_id=_PACKAGE,
        )

        assert [s.object_id for s in storages] == ["0xstorage"]

    async def test_object_with_no_type_is_excluded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty ``object_type`` must not match ``.endswith("")``-style bugs."""
        monkeypatch.setattr(storage_ops, "GetObjectsOwnedByAddress", _RecordingCommand)
        untyped = _FakeObject(object_id="0xuntyped", json=None, object_type="")
        client = _FakeListClient(
            response=SuiRpcResult(
                True,
                "",
                _FakeListResultData(
                    objects=[untyped, _storage_object(object_id="0xstorage")]
                ),
            )
        )

        storages = await list_storage_objects(
            client=client,  # type: ignore[arg-type]
            owner="0xowner",
            package_id=_PACKAGE,
        )

        assert [s.object_id for s in storages] == ["0xstorage"]

    async def test_empty_listing_returns_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Owning no objects at all is not an error."""
        monkeypatch.setattr(storage_ops, "GetObjectsOwnedByAddress", _RecordingCommand)
        client = _FakeListClient(
            response=SuiRpcResult(True, "", _FakeListResultData(objects=[]))
        )

        assert (
            await list_storage_objects(
                client=client,  # type: ignore[arg-type]
                owner="0xowner",
                package_id=_PACKAGE,
            )
            == []
        )

    async def test_unreadable_entry_is_skipped_not_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The skip-vs-raise asymmetry holds on this path too.

        An entry matching the type suffix but carrying no JSON view is
        dropped, exactly as on the PRODUCTION path.
        """
        monkeypatch.setattr(storage_ops, "GetObjectsOwnedByAddress", _RecordingCommand)
        unreadable = _FakeObject(
            object_id="0xbroken",
            json=None,
            object_type=f"{_PACKAGE}::storage_resource::Storage",
        )
        client = _FakeListClient(
            response=SuiRpcResult(
                True,
                "",
                _FakeListResultData(
                    objects=[unreadable, _storage_object(object_id="0xgood")]
                ),
            )
        )

        storages = await list_storage_objects(
            client=client,  # type: ignore[arg-type]
            owner="0xowner",
            package_id=_PACKAGE,
        )

        assert [s.object_id for s in storages] == ["0xgood"]
        with pytest.raises(ValueError):
            storage_from_object(obj=unreadable)  # type: ignore[arg-type]

    async def test_failed_listing_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failed query raises rather than reporting "owns nothing"."""
        monkeypatch.setattr(storage_ops, "GetObjectsOwnedByAddress", _RecordingCommand)
        client = _FakeListClient(response=SuiRpcResult(False, "node down"))

        with pytest.raises(
            RuntimeError, match="Cannot list Storage objects for 0xowner"
        ):
            await list_storage_objects(
                client=client,  # type: ignore[arg-type]
                owner="0xowner",
                package_id=_PACKAGE,
            )


class TestResultDataclasses:
    """Shape guarantees for the returned result types."""

    def test_split_result_is_frozen(self) -> None:
        """Results describe a landed transaction and must not be edited."""
        result = SplitResult(object_id="0xa", new_object_id="0xb", digest="0xd")
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.digest = "0xother"  # type: ignore[misc]

    def test_split_result_is_keyword_only(self) -> None:
        """Positional construction is rejected, per the kwargs convention."""
        with pytest.raises(TypeError):
            SplitResult("0xa", "0xb", "0xd")  # type: ignore[misc]

    def test_storage_op_result_is_frozen(self) -> None:
        """Same immutability guarantee for the no-new-object result."""
        result = StorageOpResult(object_id="0xa", digest="0xd")
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.object_id = "0xother"  # type: ignore[misc]

    def test_storage_op_result_is_keyword_only(self) -> None:
        """Positional construction is rejected here too."""
        with pytest.raises(TypeError):
            StorageOpResult("0xa", "0xd")  # type: ignore[misc]

    def test_storage_object_round_trips_through_json(self) -> None:
        """``StorageObject`` is printed by the CLI, so its JSON shape matters."""
        storage = StorageObject(
            object_id="0xstorage",
            start_epoch=1,
            end_epoch=5,
            storage_size=4096,
        )
        assert StorageObject.from_dict(storage.to_dict()) == storage

    def test_storage_object_defaults_are_zeroed(self) -> None:
        """Defaults describe an unknown/wrapped storage, not a real one."""
        storage = StorageObject()
        assert storage.object_id == ""
        assert storage.start_epoch == 0
        assert storage.end_epoch == 0
        assert storage.storage_size == 0
