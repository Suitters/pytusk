#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for ``pytusk.core.ops.shared_blob_compose`` and
``pytusk.core.ops.shared_blob_execute``.

Mirrors ``test_blob_metadata_ops.py``'s pattern for the closest existing
precedent -- a standalone (non-Tx1) PTB compose/execute pair:

- The ``add_*`` layer is PURE PTB COMPOSITION, so a recording fake standing
  in for ``AsyncSuiTransaction`` genuinely proves target strings and
  argument order rather than faking coverage.
- The ``execute_*`` wrappers are thin, and their thinness is the thing
  worth pinning: which result dataclass comes back, and that both the
  submission-failure and on-chain-abort paths raise ``RuntimeError``.
- ``execute_share_blob`` additionally reads the newly created
  ``SharedBlob``'s object ID back from transaction effects (``new`` shares
  its output internally and returns unit to the PTB) -- covered here via a
  fake ``changed_objects`` list. The underlying lookup,
  ``find_created_shared_object_id``, has its own dedicated tests alongside
  its sibling ``find_created_object_id`` in ``test_system_ops.py``.
- ``execute_fund_shared_blob`` composes a coin-preparation step
  (``prepare_wal_coin_for_amount``) before the ``fund`` move_call. Only the
  thin-wrapper contract is pinned here (one representative exact-match
  scenario, plus the insufficient-balance/submission-failure/on-chain-abort
  paths); the full exact-match/split/merge-then-split algorithm has its own
  dedicated tests alongside its sibling ``assert_coin_usable`` in
  ``test_utils.py``.

CLI handler tests are deliberately absent -- tusky is validated
interactively (see ``test_storage_ops.py``'s own module docstring for the
same norm).
"""

import types

import pysui.sui.sui_grpc.suimsgs.sui.rpc.v2 as sui_prot
import pytest
from pysui import SuiRpcResult

from pytusk.core.ops import shared_blob_execute
from pytusk.core.ops.shared_blob_compose import (
    add_extend_shared_blob,
    add_fund_shared_blob,
    add_share_blob,
)
from pytusk.core.ops.shared_blob_execute import (
    execute_extend_shared_blob,
    execute_fund_shared_blob,
    execute_share_blob,
)
from pytusk.core.types import SharedBlobOpResult, SharedBlobReceipt

_PACKAGE = "0xpkg"
_SENDER = "0xactive"
_BLOB = "0xblob"
_SHARED_BLOB = "0xsharedblob"
_SYSTEM = "0xsystem"


class _RecordingTxn:
    """Minimal recording fake for ``AsyncSuiTransaction``.

    Captures every ``move_call``, ``split_coin``, and ``merge_coins`` call
    in one ordered list -- ``execute_fund_shared_blob`` may compose a
    coin-preparation step and the ``fund`` move_call in the same
    transaction, so ordering across command kinds must be observable as one
    timeline. ``move_calls`` is a convenience view over just the move_call
    entries, matching ``test_blob_metadata_ops.py``'s simpler
    ``txn.calls[i]["target"]`` assertion style for the pure-compose tests,
    which never split or merge.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._n = 0

    def _result(self) -> str:
        self._n += 1
        return f"result{self._n}"

    async def move_call(
        self, *, target: str, arguments: list[object], type_arguments: list[object]
    ) -> str:
        """Record the call and return a distinct per-call sentinel result."""
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

    async def split_coin(self, *, coin: object, amounts: list[object]) -> str:
        """Record the split and return a distinct per-call sentinel result."""
        result = self._result()
        self.calls.append(
            ("split_coin", {"coin": coin, "amounts": amounts, "result": result})
        )
        return result

    async def merge_coins(self, *, merge_to: object, merge_from: list[object]) -> None:
        """Record the merge. ``merge_coins``'s own result is not chainable."""
        self.calls.append(
            ("merge_coins", {"merge_to": merge_to, "merge_from": merge_from})
        )

    async def build_and_sign(self) -> dict[str, object]:
        """Return an empty txdict.

        Every test that reaches this monkeypatches ``ExecuteTransaction``
        to a stub accepting any kwargs, so the empty dict never has to
        satisfy the real command's signature.
        """
        return {}

    @property
    def move_calls(self) -> list[dict[str, object]]:
        """Only the move_call entries' payloads, in order."""
        return [payload for kind, payload in self.calls if kind == "move_call"]


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


class _FakeExecutionError:
    """Stand-in for ``sui_prot.ExecutionError``."""

    def __init__(self, *, description: str) -> None:
        self.description = description


class _FakeStatus:
    """Stand-in for ``sui_prot.ExecutionStatus``."""

    def __init__(self, *, success: bool, error: _FakeExecutionError | None = None) -> None:
        self.success = success
        self.error = error


class _FakeEffects:
    """Stand-in for ``sui_prot.TransactionEffects``."""

    def __init__(
        self,
        *,
        status: _FakeStatus | None = None,
        changed_objects: list[_FakeChangedObject] | None = None,
    ) -> None:
        self.status = status
        self.changed_objects = changed_objects or []


class _FakeResultData:
    """Stand-in for the ``result_data`` an ``ExecuteTransaction`` result carries."""

    def __init__(self, *, effects: _FakeEffects | None, digest: str) -> None:
        self.effects = effects
        self.digest = digest


class _FakePysuiConfig:
    """Stand-in for ``WalrusClient.pysui_client.config``."""

    def __init__(self, *, active_address: str) -> None:
        self.active_address = active_address


class _FakePysuiClient:
    """Stand-in for ``WalrusClient.pysui_client``."""

    def __init__(self, *, active_address: str) -> None:
        self.config = _FakePysuiConfig(active_address=active_address)


class _FakeClient:
    """Minimal ``WalrusClient``-shaped fake for the ``execute_*`` wrappers.

    ``.transaction()`` returns the supplied recording ``txn``.
    ``.execute()`` serves ``execute_responses`` in call order -- for
    ``execute_fund_shared_blob`` this is ``[GetCoinMetaData result,
    ExecuteTransaction result]`` (``prepare_wal_coin_for_amount`` calls
    ``GetCoinMetaData`` via ``.execute()`` before the PTB is submitted); for
    ``execute_share_blob``/``execute_extend_shared_blob`` it is just
    ``[ExecuteTransaction result]``. ``.execute_for_all()`` serves
    ``execute_for_all_responses`` in call order -- the
    ``GetAddressCoinBalances``/``GetCoins`` listing calls coin preparation
    makes, only relevant for fund tests.
    """

    def __init__(
        self,
        *,
        txn: _RecordingTxn,
        execute_responses: list[SuiRpcResult],
        execute_for_all_responses: list[SuiRpcResult] | None = None,
        wal_coin_type: str = "0x2::wal::WAL",
    ) -> None:
        self._txn = txn
        self._execute_responses = list(execute_responses)
        self._execute_for_all_responses = list(execute_for_all_responses or [])
        self.pysui_client = _FakePysuiClient(active_address=_SENDER)
        self.config = types.SimpleNamespace(
            network=types.SimpleNamespace(wal_coin_type=wal_coin_type)
        )
        self.transaction_calls: list[dict[str, object]] = []
        self.executed: list[object] = []
        self.execute_for_all_calls: list[object] = []

    async def transaction(
        self, *, initial_sender: str, initial_sponsor: str | None
    ) -> _RecordingTxn:
        """Record how the transaction was opened and return the fake."""
        self.transaction_calls.append(
            {"initial_sender": initial_sender, "initial_sponsor": initial_sponsor}
        )
        return self._txn

    async def execute(self, *, command: object) -> SuiRpcResult:
        """Record the dispatched command and return the next canned response."""
        self.executed.append(command)
        return self._execute_responses.pop(0)

    async def execute_for_all(self, *, command: object) -> SuiRpcResult:
        """Record the dispatched command and return the next canned response."""
        self.execute_for_all_calls.append(command)
        return self._execute_for_all_responses.pop(0)


def _ok_result(
    *, digest: str = "0xdigest", changed_objects: list[_FakeChangedObject] | None = None
) -> SuiRpcResult:
    """A submitted-and-succeeded ``ExecuteTransaction`` result."""
    return SuiRpcResult(
        True,
        "",
        _FakeResultData(
            effects=_FakeEffects(
                status=_FakeStatus(success=True), changed_objects=changed_objects
            ),
            digest=digest,
        ),
    )


def _aborted_result(*, description: str = "boom") -> SuiRpcResult:
    """A SUBMITTED result whose transaction aborted on-chain."""
    return SuiRpcResult(
        True,
        "",
        _FakeResultData(
            effects=_FakeEffects(
                status=_FakeStatus(
                    success=False, error=_FakeExecutionError(description=description)
                )
            ),
            digest="0xdigest",
        ),
    )


def _submission_failure(*, message: str = "node refused") -> SuiRpcResult:
    """A result whose SUBMISSION failed, before any on-chain execution."""
    return SuiRpcResult(False, message)


def _shared_creation_changed_objects(
    *,
    object_id: str = "0xnewshared",
    object_type: str = "0xpkg::shared_blob::SharedBlob",
) -> list[_FakeChangedObject]:
    """A ``changed_objects`` list matching a fresh ``shared_blob::new`` output."""
    return [
        _FakeChangedObject(
            object_id=object_id,
            id_operation=sui_prot.ChangedObjectIdOperation.CREATED,
            output_owner=_FakeOwner(kind=sui_prot.OwnerOwnerKind.SHARED),
            object_type=object_type,
        )
    ]


def _balances_result(*, coin_type: str = "0x2::wal::WAL") -> SuiRpcResult:
    """A ``GetAddressCoinBalances`` result carrying a single WAL entry."""
    return SuiRpcResult(
        True,
        "",
        types.SimpleNamespace(balances=[types.SimpleNamespace(coin_type=coin_type)]),
    )


def _coin_metadata_result(*, decimals: int = 9) -> SuiRpcResult:
    """A ``GetCoinMetaData`` result carrying only ``decimals``."""
    return SuiRpcResult(
        True, "", types.SimpleNamespace(metadata=types.SimpleNamespace(decimals=decimals))
    )


def _coins_result(*, coins: list[tuple[str, int]]) -> SuiRpcResult:
    """A ``GetCoins`` result carrying the given (object_id, balance) pairs."""
    objects = [
        types.SimpleNamespace(object_id=object_id, balance=balance)
        for object_id, balance in coins
    ]
    return SuiRpcResult(True, "", types.SimpleNamespace(objects=objects))


def _stub_execute_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize ``ExecuteTransaction`` construction from an empty txdict."""
    monkeypatch.setattr(
        shared_blob_execute, "ExecuteTransaction", lambda **kwargs: object()
    )


class TestAddShareBlob:
    """PTB composition for ``shared_blob::new``."""

    async def test_composes_single_move_call(self) -> None:
        """One move_call, consuming the Blob by value, no generics."""
        txn = _RecordingTxn()
        await add_share_blob(
            txn=txn,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            blob_object=_BLOB,
        )
        assert len(txn.move_calls) == 1
        assert txn.move_calls[0]["target"] == "0xpkg::shared_blob::new"
        assert txn.move_calls[0]["arguments"] == [_BLOB]
        assert txn.move_calls[0]["type_arguments"] == []


class TestAddFundSharedBlob:
    """PTB composition for ``shared_blob::fund``."""

    async def test_composes_single_move_call(self) -> None:
        """One move_call, consuming the payment coin argument by value."""
        txn = _RecordingTxn()
        await add_fund_shared_blob(
            txn=txn,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            shared_blob_object=_SHARED_BLOB,
            payment_coin="0xcoin",
        )
        assert len(txn.move_calls) == 1
        assert txn.move_calls[0]["target"] == "0xpkg::shared_blob::fund"
        assert txn.move_calls[0]["arguments"] == [_SHARED_BLOB, "0xcoin"]
        assert txn.move_calls[0]["type_arguments"] == []


class TestAddExtendSharedBlob:
    """PTB composition for ``shared_blob::extend``."""

    async def test_composes_single_move_call(self) -> None:
        """One move_call, no payment coin argument."""
        txn = _RecordingTxn()
        await add_extend_shared_blob(
            txn=txn,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            shared_blob_object=_SHARED_BLOB,
            system_object=_SYSTEM,
            extended_epochs=5,
        )
        assert len(txn.move_calls) == 1
        assert txn.move_calls[0]["target"] == "0xpkg::shared_blob::extend"
        assert txn.move_calls[0]["arguments"] == [_SHARED_BLOB, _SYSTEM, 5]
        assert txn.move_calls[0]["type_arguments"] == []


class TestExecuteShareBlob:
    """The thin ``share_blob`` wrapper: created-object read-back from effects."""

    async def test_success_returns_new_shared_object_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The newly created SharedBlob's object ID and digest come back."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(
            txn=_RecordingTxn(),
            execute_responses=[
                _ok_result(
                    digest="0xsharedigest",
                    changed_objects=_shared_creation_changed_objects(
                        object_id="0xnewshared"
                    ),
                )
            ],
        )

        result = await execute_share_blob(
            client=client,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            blob_object=_BLOB,
        )

        assert result == SharedBlobReceipt(object_id="0xnewshared", digest="0xsharedigest")

    async def test_created_object_not_found_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A successful transaction with no matching created object surfaces
        find_created_shared_object_id's own error."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(
            txn=_RecordingTxn(),
            execute_responses=[_ok_result(changed_objects=[])],
        )
        with pytest.raises(RuntimeError, match="Could not find"):
            await execute_share_blob(
                client=client,  # type: ignore[arg-type]
                package_id=_PACKAGE,
                blob_object=_BLOB,
            )

    async def test_none_sender_resolves_to_active_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An omitted sender falls back to the config's active address."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(
            txn=_RecordingTxn(),
            execute_responses=[_ok_result(changed_objects=_shared_creation_changed_objects())],
        )
        await execute_share_blob(
            client=client,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            blob_object=_BLOB,
        )
        assert client.transaction_calls[0]["initial_sender"] == _SENDER
        assert client.transaction_calls[0]["initial_sponsor"] is None

    async def test_submission_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A rejected submission surfaces as ``RuntimeError``."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(
            txn=_RecordingTxn(), execute_responses=[_submission_failure()]
        )
        with pytest.raises(RuntimeError, match="share_blob transaction failed"):
            await execute_share_blob(
                client=client,  # type: ignore[arg-type]
                package_id=_PACKAGE,
                blob_object=_BLOB,
            )

    async def test_onchain_abort_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An accepted-but-aborted transaction is NOT reported as success."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(
            txn=_RecordingTxn(),
            execute_responses=[_aborted_result(description="boom")],
        )
        with pytest.raises(RuntimeError, match="aborted on-chain"):
            await execute_share_blob(
                client=client,  # type: ignore[arg-type]
                package_id=_PACKAGE,
                blob_object=_BLOB,
            )


class TestExecuteFundSharedBlob:
    """The thin ``fund_shared_blob`` wrapper: coin preparation, then fund."""

    async def test_success_with_exact_match_coin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An owned coin whose balance equals amount exactly needs no split."""
        _stub_execute_transaction(monkeypatch)
        txn = _RecordingTxn()
        client = _FakeClient(
            txn=txn,
            execute_responses=[_coin_metadata_result(), _ok_result(digest="0xfunddigest")],
            execute_for_all_responses=[
                _balances_result(),
                _coins_result(coins=[("0xcoinexact", 1000)]),
            ],
        )

        result = await execute_fund_shared_blob(
            client=client,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            shared_blob_object=_SHARED_BLOB,
            amount=1000,
        )

        assert result == SharedBlobOpResult(object_id=_SHARED_BLOB, digest="0xfunddigest")
        assert txn.move_calls[0]["target"] == "0xpkg::shared_blob::fund"
        assert txn.move_calls[0]["arguments"] == [_SHARED_BLOB, "0xcoinexact"]
        assert not any(kind == "split_coin" for kind, _ in txn.calls)

    async def test_insufficient_balance_raises_before_submission(self) -> None:
        """An owner whose total WAL balance falls short raises before any PTB."""
        client = _FakeClient(
            txn=_RecordingTxn(),
            execute_responses=[_coin_metadata_result()],
            execute_for_all_responses=[
                _balances_result(),
                _coins_result(coins=[("0xcoinsmall", 100)]),
            ],
        )
        with pytest.raises(RuntimeError, match="less than the requested"):
            await execute_fund_shared_blob(
                client=client,  # type: ignore[arg-type]
                package_id=_PACKAGE,
                shared_blob_object=_SHARED_BLOB,
                amount=1000,
            )
        assert len(client.executed) == 1

    async def test_submission_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A rejected submission surfaces as ``RuntimeError``."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(
            txn=_RecordingTxn(),
            execute_responses=[_coin_metadata_result(), _submission_failure()],
            execute_for_all_responses=[
                _balances_result(),
                _coins_result(coins=[("0xcoinexact", 1000)]),
            ],
        )
        with pytest.raises(RuntimeError, match="fund_shared_blob transaction failed"):
            await execute_fund_shared_blob(
                client=client,  # type: ignore[arg-type]
                package_id=_PACKAGE,
                shared_blob_object=_SHARED_BLOB,
                amount=1000,
            )

    async def test_onchain_abort_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An accepted-but-aborted transaction is NOT reported as success."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(
            txn=_RecordingTxn(),
            execute_responses=[_coin_metadata_result(), _aborted_result(description="boom")],
            execute_for_all_responses=[
                _balances_result(),
                _coins_result(coins=[("0xcoinexact", 1000)]),
            ],
        )
        with pytest.raises(RuntimeError, match="aborted on-chain"):
            await execute_fund_shared_blob(
                client=client,  # type: ignore[arg-type]
                package_id=_PACKAGE,
                shared_blob_object=_SHARED_BLOB,
                amount=1000,
            )


class TestExecuteExtendSharedBlob:
    """The thin ``extend_shared_blob`` wrapper. No payment coin involved."""

    async def test_success_returns_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The extended SharedBlob's object ID and digest come back."""
        _stub_execute_transaction(monkeypatch)
        txn = _RecordingTxn()
        client = _FakeClient(
            txn=txn, execute_responses=[_ok_result(digest="0xextenddigest")]
        )

        result = await execute_extend_shared_blob(
            client=client,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            shared_blob_object=_SHARED_BLOB,
            system_object=_SYSTEM,
            extended_epochs=3,
        )

        assert result == SharedBlobOpResult(object_id=_SHARED_BLOB, digest="0xextenddigest")
        assert txn.move_calls[0]["target"] == "0xpkg::shared_blob::extend"
        assert txn.move_calls[0]["arguments"] == [_SHARED_BLOB, _SYSTEM, 3]

    async def test_submission_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A rejected submission surfaces as ``RuntimeError``."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(
            txn=_RecordingTxn(), execute_responses=[_submission_failure()]
        )
        with pytest.raises(RuntimeError, match="extend_shared_blob transaction failed"):
            await execute_extend_shared_blob(
                client=client,  # type: ignore[arg-type]
                package_id=_PACKAGE,
                shared_blob_object=_SHARED_BLOB,
                system_object=_SYSTEM,
                extended_epochs=3,
            )

    async def test_onchain_abort_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An accepted-but-aborted transaction is NOT reported as success."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(
            txn=_RecordingTxn(),
            execute_responses=[_aborted_result(description="pool underfunded")],
        )
        with pytest.raises(RuntimeError, match="aborted on-chain"):
            await execute_extend_shared_blob(
                client=client,  # type: ignore[arg-type]
                package_id=_PACKAGE,
                shared_blob_object=_SHARED_BLOB,
                system_object=_SYSTEM,
                extended_epochs=3,
            )
