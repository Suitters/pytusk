#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for ``pytusk.core.ops.blob_metadata_compose`` and
``pytusk.core.ops.blob_metadata_execute``.

Mirrors ``test_storage_ops.py``'s pattern for the closest existing
precedent -- a standalone (non-Tx1) PTB compose/execute pair:

- The ``add_*`` layer is PURE PTB COMPOSITION, so a recording fake standing
  in for ``AsyncSuiTransaction`` genuinely proves target strings, argument
  order, and call COUNT (one move_call per key/pair) rather than faking
  coverage.
- The ``execute_*`` wrappers are thin, and their thinness is the thing
  worth pinning: which result dataclass comes back, and that both the
  submission-failure and on-chain-abort paths raise ``RuntimeError``.
- ``execute_drop_blob_metadata_keys``/``execute_drop_blob_metadata_all``
  additionally carry a PRE-TRANSACTION EXISTENCE GATE (Frank, 2026-09-03):
  before composing any PTB, they call ``client.get_blob_metadata`` and
  raise ``ValueError`` -- pre-spend -- when the blob has no metadata at
  all, or (for the keys variant) when a requested key is not currently
  present. Those gate tests use a fake ``get_blob_metadata`` rather than a
  real chain call.

CLI handler tests are deliberately absent -- tusky is validated
interactively (see ``test_storage_ops.py``'s own module docstring for the
same norm).
"""

import pytest
from pysui import SuiRpcResult

from pytusk.core.ops import blob_metadata_execute
from pytusk.core.ops.blob_metadata_compose import (
    add_drop_blob_metadata_all,
    add_drop_blob_metadata_keys,
    add_set_blob_metadata,
)
from pytusk.core.ops.blob_metadata_execute import (
    execute_drop_blob_metadata_all,
    execute_drop_blob_metadata_keys,
    execute_set_blob_metadata,
    validate_blob_metadata_exists,
    validate_blob_metadata_keys_exist,
)
from pytusk.core.types import BlobMetadataOpResult
from pytusk.core.types.blob_metadata import BlobMetadata, Metadata

_PACKAGE = "0xpkg"
_SENDER = "0xactive"
_BLOB = "0xblob"


class _RecordingTxn:
    """Minimal recording fake for ``AsyncSuiTransaction``.

    Captures every ``move_call`` in order. Unlike ``test_storage_ops.py``'s
    version, no ``transfer_objects`` recording is needed -- none of the
    three Move functions targeted here return an unconsumed object result
    that must be transferred (``remove_metadata_pair``'s ``(String,
    String)`` and ``take_metadata``'s ``Metadata`` are both ``drop``-able).
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def move_call(
        self,
        *,
        target: str,
        arguments: list[object],
        type_arguments: list[object],
    ) -> str:
        """Record the call and return a distinct per-call sentinel result."""
        result = f"result{len(self.calls) + 1}"
        self.calls.append(
            {
                "target": target,
                "arguments": arguments,
                "type_arguments": type_arguments,
                "result": result,
            }
        )
        return result

    async def build_and_sign(self) -> dict[str, object]:
        """Return an empty txdict.

        Every test that reaches this monkeypatches ``ExecuteTransaction``
        to a stub accepting any kwargs, so the empty dict never has to
        satisfy the real command's signature.
        """
        return {}


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

    def __init__(self, *, status: _FakeStatus | None) -> None:
        self.status = status


class _FakeResultData:
    """Stand-in for the ``result_data`` an ``ExecuteTransaction`` carries.

    ``blob_metadata_execute`` reads exactly two attributes off it:
    ``.effects`` (via ``require_success``) and ``.digest``.
    """

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

    ``.transaction()`` returns the supplied recording ``txn`` and records
    the sender/sponsor it was opened with. ``.execute()`` returns the
    single canned ``SuiRpcResult``. ``.get_blob_metadata()`` returns the
    canned ``metadata_result`` (a ``BlobMetadata | None``), recording each
    call's ``blob_object`` -- this is the pre-transaction gate's only
    dependency on the client, for the two drop functions.
    """

    def __init__(
        self,
        *,
        txn: _RecordingTxn,
        response: SuiRpcResult,
        metadata_result: BlobMetadata | None = None,
    ) -> None:
        self._txn = txn
        self._response = response
        self._metadata_result = metadata_result
        self.pysui_client = _FakePysuiClient(active_address=_SENDER)
        self.transaction_calls: list[dict[str, object]] = []
        self.executed: list[object] = []
        self.get_blob_metadata_calls: list[str] = []

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

    async def get_blob_metadata(self, *, blob_object: str) -> BlobMetadata | None:
        """Record the call and return the canned metadata result."""
        self.get_blob_metadata_calls.append(blob_object)
        return self._metadata_result


def _ok_result(*, digest: str = "0xdigest") -> SuiRpcResult:
    """A submitted-and-succeeded ``ExecuteTransaction`` result."""
    return SuiRpcResult(
        True,
        "",
        _FakeResultData(effects=_FakeEffects(status=_FakeStatus(success=True)), digest=digest),
    )


def _aborted_result(*, description: str = "EMissingMetadata") -> SuiRpcResult:
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


def _stub_execute_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize ``ExecuteTransaction`` construction from an empty txdict."""
    monkeypatch.setattr(
        blob_metadata_execute, "ExecuteTransaction", lambda **kwargs: object()
    )


class TestAddSetBlobMetadata:
    """PTB composition for ``blob::insert_or_update_metadata_pair``."""

    async def test_one_call_per_pair(self) -> None:
        """One move_call per pair, in mapping iteration order."""
        txn = _RecordingTxn()
        await add_set_blob_metadata(
            txn=txn,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            blob_object=_BLOB,
            pairs={"content-type": "text/plain", "link": "https://example.com"},
        )
        assert len(txn.calls) == 2
        assert txn.calls[0]["target"] == "0xpkg::blob::insert_or_update_metadata_pair"
        assert txn.calls[0]["arguments"] == [_BLOB, "content-type", "text/plain"]
        assert txn.calls[1]["arguments"] == [_BLOB, "link", "https://example.com"]
        assert all(call["type_arguments"] == [] for call in txn.calls)

    async def test_empty_pairs_composes_nothing(self) -> None:
        """No pairs, no move_calls."""
        txn = _RecordingTxn()
        await add_set_blob_metadata(
            txn=txn,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            blob_object=_BLOB,
            pairs={},
        )
        assert txn.calls == []


class TestAddDropBlobMetadataKeys:
    """PTB composition for ``blob::remove_metadata_pair``."""

    async def test_one_call_per_key(self) -> None:
        """One move_call per key, in sequence order."""
        txn = _RecordingTxn()
        await add_drop_blob_metadata_keys(
            txn=txn,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            blob_object=_BLOB,
            keys=["content-type", "link"],
        )
        assert len(txn.calls) == 2
        assert txn.calls[0]["target"] == "0xpkg::blob::remove_metadata_pair"
        assert txn.calls[0]["arguments"] == [_BLOB, "content-type"]
        assert txn.calls[1]["arguments"] == [_BLOB, "link"]
        assert all(call["type_arguments"] == [] for call in txn.calls)

    async def test_empty_keys_composes_nothing(self) -> None:
        """No keys, no move_calls."""
        txn = _RecordingTxn()
        await add_drop_blob_metadata_keys(
            txn=txn,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            blob_object=_BLOB,
            keys=[],
        )
        assert txn.calls == []


class TestAddDropBlobMetadataAll:
    """PTB composition for ``blob::take_metadata``."""

    async def test_composes_single_move_call(self) -> None:
        """One move_call taking the blob by mutable reference, no generics."""
        txn = _RecordingTxn()
        await add_drop_blob_metadata_all(
            txn=txn,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            blob_object=_BLOB,
        )
        assert len(txn.calls) == 1
        assert txn.calls[0]["target"] == "0xpkg::blob::take_metadata"
        assert txn.calls[0]["arguments"] == [_BLOB]
        assert txn.calls[0]["type_arguments"] == []


class TestValidateBlobMetadataKeysExist:
    """The extracted pre-transaction existence gate for --keys, standalone.

    ``execute_drop_blob_metadata_keys``'s own gate tests (below) already
    prove this logic behaves identically when called through that wrapper
    -- this class pins the extracted function's contract directly, since
    it is now also the CLI's own gate call (``tusky drop_blob_metadata
    --keys``, which bypasses the wrapper for ``--mode``).
    """

    async def test_raises_when_no_metadata_at_all(self) -> None:
        """``get_blob_metadata`` returning ``None`` raises."""
        client = _FakeClient(
            txn=_RecordingTxn(), response=_ok_result(), metadata_result=None
        )

        with pytest.raises(ValueError, match="no metadata set at all"):
            await validate_blob_metadata_keys_exist(
                client=client,  # type: ignore[arg-type]
                blob_object=_BLOB,
                keys=["content-type"],
            )

    async def test_raises_when_a_requested_key_is_absent(self) -> None:
        """A key not currently present raises, naming it."""
        client = _FakeClient(
            txn=_RecordingTxn(),
            response=_ok_result(),
            metadata_result=BlobMetadata(data=[Metadata(key="content-type", value="x")]),
        )

        with pytest.raises(ValueError, match="link"):
            await validate_blob_metadata_keys_exist(
                client=client,  # type: ignore[arg-type]
                blob_object=_BLOB,
                keys=["content-type", "link"],
            )

    async def test_passes_when_all_keys_present(self) -> None:
        """All requested keys present: returns ``None``, no raise."""
        client = _FakeClient(
            txn=_RecordingTxn(),
            response=_ok_result(),
            metadata_result=BlobMetadata(
                data=[
                    Metadata(key="content-type", value="x"),
                    Metadata(key="link", value="y"),
                ]
            ),
        )

        result = await validate_blob_metadata_keys_exist(
            client=client,  # type: ignore[arg-type]
            blob_object=_BLOB,
            keys=["content-type", "link"],
        )

        assert result is None
        assert client.get_blob_metadata_calls == [_BLOB]


class TestValidateBlobMetadataExists:
    """The extracted pre-transaction existence gate for --all, standalone.

    ``execute_drop_blob_metadata_all``'s own gate tests (below) already
    prove this logic behaves identically when called through that wrapper
    -- this class pins the extracted function's contract directly, since
    it is now also the CLI's own gate call (``tusky drop_blob_metadata
    --all``, which bypasses the wrapper for ``--mode``).
    """

    async def test_raises_when_metadata_is_none(self) -> None:
        """``get_blob_metadata`` returning ``None`` raises."""
        client = _FakeClient(
            txn=_RecordingTxn(), response=_ok_result(), metadata_result=None
        )

        with pytest.raises(ValueError, match="no metadata set at all"):
            await validate_blob_metadata_exists(
                client=client,  # type: ignore[arg-type]
                blob_object=_BLOB,
            )

    async def test_raises_when_metadata_is_empty(self) -> None:
        """An empty pair list is treated the same as no metadata at all."""
        client = _FakeClient(
            txn=_RecordingTxn(),
            response=_ok_result(),
            metadata_result=BlobMetadata(data=[]),
        )

        with pytest.raises(ValueError, match="no metadata set at all"):
            await validate_blob_metadata_exists(
                client=client,  # type: ignore[arg-type]
                blob_object=_BLOB,
            )

    async def test_passes_when_metadata_present(self) -> None:
        """Metadata present: returns ``None``, no raise."""
        client = _FakeClient(
            txn=_RecordingTxn(),
            response=_ok_result(),
            metadata_result=BlobMetadata(data=[Metadata(key="content-type", value="x")]),
        )

        result = await validate_blob_metadata_exists(
            client=client,  # type: ignore[arg-type]
            blob_object=_BLOB,
        )

        assert result is None
        assert client.get_blob_metadata_calls == [_BLOB]


class TestExecuteSetBlobMetadata:
    """The thin ``set_blob_metadata`` wrapper. No pre-transaction gate."""

    async def test_success_returns_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The acted-on object_id and digest come back."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(txn=_RecordingTxn(), response=_ok_result(digest="0xsetdigest"))

        result = await execute_set_blob_metadata(
            client=client,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            blob_object=_BLOB,
            pairs={"content-type": "text/plain"},
        )

        assert result == BlobMetadataOpResult(object_id=_BLOB, digest="0xsetdigest")

    async def test_no_gate_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Upsert semantics need no existence check -- get_blob_metadata unused."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(txn=_RecordingTxn(), response=_ok_result())

        await execute_set_blob_metadata(
            client=client,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            blob_object=_BLOB,
            pairs={"content-type": "text/plain"},
        )

        assert client.get_blob_metadata_calls == []

    async def test_none_sender_resolves_to_active_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An omitted sender falls back to the config's active address."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(txn=_RecordingTxn(), response=_ok_result())

        await execute_set_blob_metadata(
            client=client,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            blob_object=_BLOB,
            pairs={"k": "v"},
        )

        assert client.transaction_calls[0]["initial_sender"] == _SENDER
        assert client.transaction_calls[0]["initial_sponsor"] is None

    async def test_sponsor_is_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A sponsor reaches the transaction it was meant to sponsor."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(txn=_RecordingTxn(), response=_ok_result())

        await execute_set_blob_metadata(
            client=client,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            blob_object=_BLOB,
            pairs={"k": "v"},
            sponsor="0xsponsor",
        )

        assert client.transaction_calls[0]["initial_sponsor"] == "0xsponsor"

    async def test_submission_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A rejected submission surfaces as ``RuntimeError``."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(txn=_RecordingTxn(), response=_submission_failure())
        with pytest.raises(RuntimeError, match="set_blob_metadata transaction failed"):
            await execute_set_blob_metadata(
                client=client,  # type: ignore[arg-type]
                package_id=_PACKAGE,
                blob_object=_BLOB,
                pairs={"k": "v"},
            )

    async def test_onchain_abort_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An accepted-but-aborted transaction is NOT reported as success."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(
            txn=_RecordingTxn(), response=_aborted_result(description="boom")
        )
        with pytest.raises(RuntimeError, match="aborted on-chain"):
            await execute_set_blob_metadata(
                client=client,  # type: ignore[arg-type]
                package_id=_PACKAGE,
                blob_object=_BLOB,
                pairs={"k": "v"},
            )


class TestExecuteDropBlobMetadataKeys:
    """The thin ``drop_blob_metadata_keys`` wrapper and its existence gate."""

    async def test_gate_raises_when_no_metadata_at_all(self) -> None:
        """``get_blob_metadata`` returning ``None`` raises before any PTB."""
        txn = _RecordingTxn()
        client = _FakeClient(txn=txn, response=_ok_result(), metadata_result=None)

        with pytest.raises(ValueError, match="no metadata set at all"):
            await execute_drop_blob_metadata_keys(
                client=client,  # type: ignore[arg-type]
                package_id=_PACKAGE,
                blob_object=_BLOB,
                keys=["content-type"],
            )

        assert client.executed == []
        assert txn.calls == []

    async def test_gate_raises_when_a_requested_key_is_absent(self) -> None:
        """A key not currently present raises before any PTB, naming it."""
        txn = _RecordingTxn()
        client = _FakeClient(
            txn=txn,
            response=_ok_result(),
            metadata_result=BlobMetadata(data=[Metadata(key="content-type", value="x")]),
        )

        with pytest.raises(ValueError, match="link"):
            await execute_drop_blob_metadata_keys(
                client=client,  # type: ignore[arg-type]
                package_id=_PACKAGE,
                blob_object=_BLOB,
                keys=["content-type", "link"],
            )

        assert client.executed == []
        assert txn.calls == []

    async def test_gate_passes_and_executes_when_all_keys_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All requested keys present: composes and submits normally."""
        _stub_execute_transaction(monkeypatch)
        txn = _RecordingTxn()
        client = _FakeClient(
            txn=txn,
            response=_ok_result(digest="0xdropdigest"),
            metadata_result=BlobMetadata(
                data=[
                    Metadata(key="content-type", value="x"),
                    Metadata(key="link", value="y"),
                ]
            ),
        )

        result = await execute_drop_blob_metadata_keys(
            client=client,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            blob_object=_BLOB,
            keys=["content-type", "link"],
        )

        assert result == BlobMetadataOpResult(object_id=_BLOB, digest="0xdropdigest")
        assert client.get_blob_metadata_calls == [_BLOB]
        assert len(txn.calls) == 2
        assert txn.calls[0]["target"] == "0xpkg::blob::remove_metadata_pair"

    async def test_submission_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A rejected submission surfaces as ``RuntimeError``, gate having passed."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(
            txn=_RecordingTxn(),
            response=_submission_failure(),
            metadata_result=BlobMetadata(data=[Metadata(key="content-type", value="x")]),
        )
        with pytest.raises(
            RuntimeError, match="drop_blob_metadata_keys transaction failed"
        ):
            await execute_drop_blob_metadata_keys(
                client=client,  # type: ignore[arg-type]
                package_id=_PACKAGE,
                blob_object=_BLOB,
                keys=["content-type"],
            )

    async def test_onchain_abort_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An accepted-but-aborted transaction is NOT reported as success."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(
            txn=_RecordingTxn(),
            response=_aborted_result(description="EMissingMetadata"),
            metadata_result=BlobMetadata(data=[Metadata(key="content-type", value="x")]),
        )
        with pytest.raises(RuntimeError, match="aborted on-chain"):
            await execute_drop_blob_metadata_keys(
                client=client,  # type: ignore[arg-type]
                package_id=_PACKAGE,
                blob_object=_BLOB,
                keys=["content-type"],
            )


class TestExecuteDropBlobMetadataAll:
    """The thin ``drop_blob_metadata_all`` wrapper and its existence gate."""

    async def test_gate_raises_when_metadata_is_none(self) -> None:
        """``get_blob_metadata`` returning ``None`` raises before any PTB."""
        txn = _RecordingTxn()
        client = _FakeClient(txn=txn, response=_ok_result(), metadata_result=None)

        with pytest.raises(ValueError, match="no metadata set at all"):
            await execute_drop_blob_metadata_all(
                client=client,  # type: ignore[arg-type]
                package_id=_PACKAGE,
                blob_object=_BLOB,
            )

        assert client.executed == []
        assert txn.calls == []

    async def test_gate_raises_when_metadata_is_empty(self) -> None:
        """An empty pair list is treated the same as no metadata at all."""
        txn = _RecordingTxn()
        client = _FakeClient(
            txn=txn, response=_ok_result(), metadata_result=BlobMetadata(data=[])
        )

        with pytest.raises(ValueError, match="no metadata set at all"):
            await execute_drop_blob_metadata_all(
                client=client,  # type: ignore[arg-type]
                package_id=_PACKAGE,
                blob_object=_BLOB,
            )

        assert client.executed == []
        assert txn.calls == []

    async def test_gate_passes_and_executes_when_metadata_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Metadata present: composes and submits normally."""
        _stub_execute_transaction(monkeypatch)
        txn = _RecordingTxn()
        client = _FakeClient(
            txn=txn,
            response=_ok_result(digest="0xdropalldigest"),
            metadata_result=BlobMetadata(data=[Metadata(key="content-type", value="x")]),
        )

        result = await execute_drop_blob_metadata_all(
            client=client,  # type: ignore[arg-type]
            package_id=_PACKAGE,
            blob_object=_BLOB,
        )

        assert result == BlobMetadataOpResult(object_id=_BLOB, digest="0xdropalldigest")
        assert client.get_blob_metadata_calls == [_BLOB]
        assert len(txn.calls) == 1
        assert txn.calls[0]["target"] == "0xpkg::blob::take_metadata"

    async def test_submission_failure_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A rejected submission surfaces as ``RuntimeError``, gate having passed."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(
            txn=_RecordingTxn(),
            response=_submission_failure(),
            metadata_result=BlobMetadata(data=[Metadata(key="content-type", value="x")]),
        )
        with pytest.raises(
            RuntimeError, match="drop_blob_metadata_all transaction failed"
        ):
            await execute_drop_blob_metadata_all(
                client=client,  # type: ignore[arg-type]
                package_id=_PACKAGE,
                blob_object=_BLOB,
            )

    async def test_onchain_abort_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An accepted-but-aborted transaction is NOT reported as success."""
        _stub_execute_transaction(monkeypatch)
        client = _FakeClient(
            txn=_RecordingTxn(),
            response=_aborted_result(description="EMissingMetadata"),
            metadata_result=BlobMetadata(data=[Metadata(key="content-type", value="x")]),
        )
        with pytest.raises(RuntimeError, match="aborted on-chain"):
            await execute_drop_blob_metadata_all(
                client=client,  # type: ignore[arg-type]
                package_id=_PACKAGE,
                blob_object=_BLOB,
            )
