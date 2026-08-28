#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for tusky command handlers.

Currently covers only ``certify_blob``'s ``--recover`` mode (re-uploading
slivers before collecting confirmations, for a blob whose sliver fan-out
never ran).
"""

import argparse
import types
from typing import Any

import pysui.sui.sui_grpc.suimsgs.sui.rpc.v2 as sui_prot
import pytest
from pysui import SuiRpcResult

from pytusk import NativeBlobReceipt, StageTimings, StorageObject
from pytusk.core.encoding import EncodedBlob
from pytusk.core.ops import blob_execute as blob_execute_module
from pytusk.tusky import tusky_cmds_common, tusky_cmds_native_upload, tusky_cmds_storage


class _FakeValue:
    """Stand-in for a protobuf ``Value`` where only the set field exists."""

    def __init__(self, **fields: object) -> None:
        for name, value in fields.items():
            setattr(self, name, value)


class _FakeStruct:
    """Stand-in for a protobuf ``Struct``."""

    def __init__(self, *, fields: dict[str, object]) -> None:
        self.fields = fields


class _FakeJson:
    """Stand-in for ``Object.json``."""

    def __init__(self, *, struct_value: _FakeStruct) -> None:
        self.struct_value = struct_value


class _FakeObject:
    """Stand-in for ``sui_prot.Object``, holding only what certify_blob reads."""

    def __init__(self, *, object_id: str, object_type: str, json: _FakeJson) -> None:
        self.object_id = object_id
        self.object_type = object_type
        self.json = json


def _blob_object(
    *, blob_id_bytes: bytes, end_epoch: int = 42, deletable: bool = True
) -> _FakeObject:
    """Build a fake on-chain Blob object carrying the given raw blob_id."""
    blob_id_int = int.from_bytes(blob_id_bytes, byteorder="little")
    fields: dict[str, object] = {
        "blob_id": _FakeValue(string_value=str(blob_id_int)),
        "storage": _FakeValue(
            struct_value=_FakeStruct(
                fields={"end_epoch": _FakeValue(number_value=float(end_epoch))}
            )
        ),
        "deletable": _FakeValue(bool_value=deletable),
    }
    return _FakeObject(
        object_id="0xblob",
        object_type="0xpkg::blob::Blob",
        json=_FakeJson(struct_value=_FakeStruct(fields=fields)),
    )


class _FakeNetwork:
    def __init__(self) -> None:
        self.system_object = "0xsystem"
        self.staking_object = "0xstaking"


class _FakePytuskConfig:
    def __init__(self) -> None:
        self.network = _FakeNetwork()


class _FakePysuiConfig:
    def __init__(self) -> None:
        self.active_address = "0xactive"


class _FakePysuiClient:
    def __init__(self) -> None:
        self.config = _FakePysuiConfig()


class _FakeWalrusClient:
    """Fake ``WalrusClient``-shaped async context manager for certify_blob tests.

    Implements only what certify_blob's --recover path touches: object
    fetch, committee, and the config/pysui_client attributes
    ``resolve_sender`` and ``walrus_package_id`` read.
    """

    def __init__(self, *, blob_object: _FakeObject, n_shards: int = 7) -> None:
        self._blob_object = blob_object
        self._n_shards = n_shards
        self.config = _FakePytuskConfig()
        self.pysui_client = _FakePysuiClient()

    async def __aenter__(self) -> "_FakeWalrusClient":  # noqa: PYI034 -- typing.Self is 3.11+ only (project targets >=3.10.6); typing_extensions.Self would add an undeclared dependency, and a bound TypeVar here immediately trips PYI019 instead
        """Enter the fake client's async context, returning itself."""
        return self

    async def __aexit__(
        self, exc_type: object, exc_val: object, exc_tb: object
    ) -> None:
        """Exit the fake client's async context; nothing to clean up."""
        return

    async def execute(self, *, command: object) -> SuiRpcResult:
        """Return the canned blob object for any dispatched command."""
        return SuiRpcResult(True, "", self._blob_object)

    async def committee(self) -> object:
        """Return a minimal object exposing only ``n_shards``."""
        return types.SimpleNamespace(n_shards=self._n_shards)


def _base_args(**overrides: object) -> argparse.Namespace:
    """Build a complete argparse.Namespace for certify_blob, with defaults
    for every attribute the handler and its helpers read, overridable per
    test."""
    defaults: dict[str, object] = {
        "blobid": "0xblob",
        "recover": False,
        "content": None,
        "file": None,
        "sender": None,
        "sponsor": None,
        "mode": "execute",
        "from_cfg_path": None,
        "active_network": None,
        "pysui_config_path": None,
        "pysui_group_name": None,
        "pysui_profile_name": None,
        "pysui_address": None,
        "pysui_alias": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _patch_pipeline(
    *,
    monkeypatch: pytest.MonkeyPatch,
    fake_client: _FakeWalrusClient,
    encoded_blob_id: bytes,
    upload_calls: list[object],
) -> None:
    """Patch every collaborator certify_blob's --recover path calls, so
    only the handler's own orchestration logic (validation, blob_id-match
    check, call order) is under test."""
    monkeypatch.setattr(
        tusky_cmds_common, "PytuskConfiguration", lambda **kwargs: object()
    )
    monkeypatch.setattr(
        tusky_cmds_native_upload, "WalrusClient", lambda *, pytusk_config: fake_client
    )

    async def _fake_resolve_package_id(*, client: object, system_object: str) -> str:
        return "0xwalruspkg"

    monkeypatch.setattr(
        tusky_cmds_common, "resolve_package_id", _fake_resolve_package_id
    )

    def _fake_encode_blob(*, data: bytes, n_shards: int) -> object:
        return types.SimpleNamespace(blob_id=encoded_blob_id)

    monkeypatch.setattr(tusky_cmds_native_upload, "encode_blob", _fake_encode_blob)

    async def _fake_upload_slivers(
        *, client: object, committee: object, encoded: object
    ) -> object:
        upload_calls.append(encoded)
        return object()

    monkeypatch.setattr(
        tusky_cmds_native_upload, "upload_slivers", _fake_upload_slivers
    )

    async def _fake_collect_confirmations(**kwargs: object) -> str:
        return "fake-certificate"

    monkeypatch.setattr(
        tusky_cmds_native_upload, "collect_confirmations", _fake_collect_confirmations
    )

    async def _fake_certify(**kwargs: object) -> NativeBlobReceipt:
        return NativeBlobReceipt(
            blob_id="fake-blob-id-b64",
            object_id="0xblob",
            certified=True,
            end_epoch=42,
            failed_stage=None,
            timings=StageTimings(
                encode=None,
                register_tx1=None,
                sliver_upload=None,
                confirmations=None,
                certify_tx2=0.1,
                total=None,
            ),
        )

    monkeypatch.setattr(tusky_cmds_native_upload, "certify", _fake_certify)


class TestCertifyBlobRecover:
    """Tests for certify_blob's --recover mode (sliver re-upload before
    confirmation collection)."""

    async def test_recover_without_source_errors(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--recover without --content/--file must error before touching
        any client -- no WalrusClient/config is even referenced."""
        args = _base_args(recover=True, content=None, file=None)

        with pytest.raises(SystemExit) as excinfo:
            await tusky_cmds_native_upload.certify_blob(args)

        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "--recover requires --content or --file" in captured.err

    async def test_source_without_recover_errors(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--content/--file without --recover must error, not silently
        switch modes."""
        args = _base_args(recover=False, content="hello", file=None)

        with pytest.raises(SystemExit) as excinfo:
            await tusky_cmds_native_upload.certify_blob(args)

        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "--content/--file require --recover" in captured.err

    async def test_recover_blob_id_mismatch_errors_before_upload(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A re-encoded blob_id that does not match the on-chain
        registration must error out WITHOUT calling upload_slivers."""
        on_chain_blob_id = b"\x01" * 32
        mismatched_blob_id = b"\x02" * 32
        fake_client = _FakeWalrusClient(
            blob_object=_blob_object(blob_id_bytes=on_chain_blob_id)
        )
        upload_calls: list[object] = []
        _patch_pipeline(
            monkeypatch=monkeypatch,
            fake_client=fake_client,
            encoded_blob_id=mismatched_blob_id,
            upload_calls=upload_calls,
        )
        args = _base_args(recover=True, content="hello world")

        with pytest.raises(SystemExit) as excinfo:
            await tusky_cmds_native_upload.certify_blob(args)

        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "does not match the blob_id already registered" in captured.err
        assert upload_calls == []

    async def test_recover_happy_path_uploads_slivers_then_certifies(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A matching blob_id calls upload_slivers before falling through
        to the existing collect_confirmations/certify flow."""
        blob_id_bytes = b"\x01" * 32
        fake_client = _FakeWalrusClient(
            blob_object=_blob_object(blob_id_bytes=blob_id_bytes)
        )
        upload_calls: list[object] = []
        _patch_pipeline(
            monkeypatch=monkeypatch,
            fake_client=fake_client,
            encoded_blob_id=blob_id_bytes,
            upload_calls=upload_calls,
        )
        args = _base_args(recover=True, content="hello world")

        await tusky_cmds_native_upload.certify_blob(args)

        assert len(upload_calls) == 1
        captured = capsys.readouterr()
        assert '"certified": true' in captured.out


class _FakeNativeSimulateTxn:
    """Recording fake for ``AsyncSuiTransaction`` covering ``store_blob_native``'s
    simulate-mode Tx1 composition. No tip is ever composed on the native
    path, so unlike the relay-side fakes this needs no real
    ``ProgrammableTransactionBuilder`` -- only ``move_call``/
    ``transfer_objects``/``build_and_sign``, exactly what
    ``add_registration_sequence(tip=None)`` calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def move_call(
        self, *, target: str, arguments: list[object], type_arguments: list[object]
    ) -> str:
        self.calls.append(("move_call", {"target": target, "arguments": arguments}))
        return "result"

    async def transfer_objects(self, *, transfers: list[object], recipient: str) -> None:
        self.calls.append(("transfer_objects", {"transfers": transfers, "recipient": recipient}))

    async def build_and_sign(self) -> dict[str, bytes]:
        return {"tx_bytestr": b"fake"}


class _FakePysuiConfigNoSponsorCheck:
    """``PysuiConfiguration`` stub whose ``keypair_for_address`` raises if
    it is EVER called -- used to prove store_blob_native's simulate branch
    never performs a sponsor-signability preflight, matching
    ``execute_reserve_and_register``'s own deliberate omission (see
    ``TestExecuteReserveAndRegisterSponsorPreflightSkipped`` in
    ``test_system_ops.py``)."""

    def __init__(self, *, active_address: str) -> None:
        self.active_address = active_address

    def keypair_for_address(self, *, address: str) -> object:
        raise AssertionError(
            "keypair_for_address must not be called on the native "
            "store_blob_native simulate path -- a sponsor absent from "
            "local config may legitimately sign out-of-band."
        )

    def alias_for_address(self, *, address: str) -> str:
        """No-op address validation -- every 0x-address is accepted."""
        return address


class _FakeNativeSimulateClient:
    """``WalrusClient``-shaped async context manager for
    ``store_blob_native``'s simulate mode."""

    def __init__(self, *, active_address: str) -> None:
        self.config = types.SimpleNamespace(
            active_network="testnet",
            network=types.SimpleNamespace(
                system_object="0xsystem", wal_coin_type="0x2::wal::WAL"
            ),
        )
        self.pysui_client = types.SimpleNamespace(
            config=_FakePysuiConfigNoSponsorCheck(active_address=active_address)
        )
        self.txns: list[_FakeNativeSimulateTxn] = []

    async def __aenter__(self) -> "_FakeNativeSimulateClient":
        """Enter the fake client's async context, returning itself."""
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        """Exit the fake client's async context; nothing to clean up."""
        return

    async def committee(self) -> types.SimpleNamespace:
        """Return a minimal object exposing only ``n_shards``."""
        return types.SimpleNamespace(n_shards=1000)

    async def transaction(self, **kwargs: Any) -> _FakeNativeSimulateTxn:
        """Open (and record) a fake composable transaction."""
        txn = _FakeNativeSimulateTxn()
        self.txns.append(txn)
        return txn


def _base_native_args(**overrides: object) -> argparse.Namespace:
    """Build a complete argparse.Namespace for store_blob_native, with
    defaults for every attribute the handler and its helpers read."""
    defaults: dict[str, object] = {
        "content": "hello world",
        "file": None,
        "epochs": 3,
        "permanent": False,
        "recipient": None,
        "full_json": False,
        "log_file": None,
        "verbose": False,
        "sender": None,
        "sponsor": None,
        "mode": "simulate",
        "from_cfg_path": None,
        "active_network": None,
        "pysui_config_path": None,
        "pysui_group_name": None,
        "pysui_profile_name": None,
        "pysui_address": None,
        "pysui_alias": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestStoreBlobNativeSimulateSkipsSponsorPreflight:
    """``store_blob_native``'s simulate branch composes Tx1 via
    ``add_registration_sequence(tip=None)`` and ``preflight_payment``, but
    deliberately does NOT call ``preflight_sponsor`` -- matching
    ``execute_reserve_and_register``, which never validates sponsor
    signability because a sponsor may legitimately sign out-of-band with
    no local keypair. This mirrors ``TestExecuteReserveAndRegisterSponsorPreflightSkipped``
    (``test_system_ops.py``) at the CLI layer."""

    async def test_unsignable_sponsor_still_completes_simulate(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A --sponsor absent from local config (keypair_for_address would
        raise AssertionError if ever consulted -- see
        ``_FakePysuiConfigNoSponsorCheck``) must NOT be rejected: simulate
        completes normally and prints its cost summary, proof no
        sponsor-signability check ran anywhere in this path."""
        encoded = EncodedBlob(
            blob_id=b"\x01" * 32,
            root_hash=b"\x02" * 32,
            unencoded_length=11,
            n_shards=1000,
            slivers=(),
            metadata_bcs=b"",
        )
        client = _FakeNativeSimulateClient(active_address="0xsender")

        monkeypatch.setattr(
            tusky_cmds_native_upload, "config_from_args", lambda args: object()
        )
        monkeypatch.setattr(
            tusky_cmds_native_upload, "WalrusClient", lambda *, pytusk_config: client
        )
        monkeypatch.setattr(
            tusky_cmds_native_upload,
            "encode_blob",
            lambda *, data, n_shards: encoded,
        )

        async def _fake_walrus_package_id(*, client: object) -> tuple[str, str]:
            return "0xsystem", "0xpkg"

        monkeypatch.setattr(
            tusky_cmds_native_upload, "walrus_package_id", _fake_walrus_package_id
        )

        async def _fake_select_wal_payment_coin(*, client: object, owner: str) -> str:
            return "0xwal"

        monkeypatch.setattr(
            blob_execute_module, "select_wal_payment_coin", _fake_select_wal_payment_coin
        )

        async def _fake_submit(*, client: object, txdict: dict, mode: str) -> SuiRpcResult:
            return SuiRpcResult(True, "", types.SimpleNamespace())

        monkeypatch.setattr(tusky_cmds_native_upload, "submit", _fake_submit)

        async def _fake_cost(
            *, client: object, transaction: object
        ) -> tuple[dict, dict]:
            return {}, {}

        monkeypatch.setattr(
            tusky_cmds_native_upload, "simulate_cost_from_balance_changes", _fake_cost
        )

        args = _base_native_args(sponsor="0xstranger")

        await tusky_cmds_native_upload.store_blob_native(args)

        captured = capsys.readouterr()
        assert '"mode": "simulate"' in captured.out
        assert len(client.txns) == 1
        kinds = [kind for kind, _ in client.txns[0].calls]
        assert kinds == ["move_call", "move_call", "transfer_objects"]


class TestSplitByEpochApplicable:
    """``_split_by_epoch_applicable`` requires an epoch range spanning at
    least 2 epochs (an interior split point must exist)."""

    def test_true_when_range_spans_two_epochs(self) -> None:
        storage = StorageObject(
            object_id="0x1", start_epoch=491, end_epoch=493, storage_size=100
        )
        assert tusky_cmds_storage._split_by_epoch_applicable(storage=storage) is True

    def test_false_when_range_spans_one_epoch(self) -> None:
        storage = StorageObject(
            object_id="0x1", start_epoch=491, end_epoch=492, storage_size=100
        )
        assert tusky_cmds_storage._split_by_epoch_applicable(storage=storage) is False


class TestSplitBySizeApplicable:
    """``_split_by_size_applicable`` requires a storage size of at least 2
    bytes (so both halves of a split are non-empty)."""

    def test_true_when_size_is_two(self) -> None:
        storage = StorageObject(
            object_id="0x1", start_epoch=491, end_epoch=492, storage_size=2
        )
        assert tusky_cmds_storage._split_by_size_applicable(storage=storage) is True

    def test_false_when_size_is_one(self) -> None:
        storage = StorageObject(
            object_id="0x1", start_epoch=491, end_epoch=492, storage_size=1
        )
        assert tusky_cmds_storage._split_by_size_applicable(storage=storage) is False

    def test_false_when_size_is_zero(self) -> None:
        storage = StorageObject(
            object_id="0x1", start_epoch=491, end_epoch=492, storage_size=0
        )
        assert tusky_cmds_storage._split_by_size_applicable(storage=storage) is False


class _FakeDestroyTxn:
    """Fake ``AsyncSuiTransaction`` recording ``move_call`` args for
    ``_destroy_storage_batches``."""

    def __init__(self) -> None:
        self.move_calls: list[dict[str, object]] = []

    async def move_call(self, **kwargs: object) -> None:
        self.move_calls.append(kwargs)

    async def build_and_sign(self) -> dict[str, object]:
        return {"tx_bytestr": b"fake"}


class _FakeDestroyClient:
    """Fake ``WalrusClient`` for ``_destroy_storage_batches`` -- one canned
    ``execute()`` response per call, in call order."""

    def __init__(self, *, responses: list[SuiRpcResult]) -> None:
        self._responses = list(responses)
        self.txns: list[_FakeDestroyTxn] = []

    async def transaction(self, **kwargs: object) -> _FakeDestroyTxn:
        txn = _FakeDestroyTxn()
        self.txns.append(txn)
        return txn

    async def execute(self, **kwargs: object) -> SuiRpcResult:
        return self._responses.pop(0)


class TestDestroyStorageBatches:
    """``_destroy_storage_batches`` -- batching, the per-object rebate
    printout, the execute/simulate mode split, and the total-rebate flag
    (a legitimate 0 MIST total must still print, not be suppressed)."""

    async def test_execute_mode_prints_per_object_and_total_rebate(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            tusky_cmds_common, "ExecuteTransaction", lambda **kwargs: object()
        )
        executed = sui_prot.ExecutedTransaction(
            digest="0xdigest",
            effects=sui_prot.TransactionEffects(
                gas_used=sui_prot.GasCostSummary(storage_rebate=2979200)
            ),
            objects=sui_prot.ObjectSet(
                objects=[
                    sui_prot.Object(
                        object_id="0x" + "0" * 63 + "a", storage_rebate=1489600
                    ),
                    sui_prot.Object(
                        object_id="0x" + "0" * 63 + "b", storage_rebate=1489600
                    ),
                ]
            ),
        )
        client = _FakeDestroyClient(responses=[SuiRpcResult(True, "", executed)])

        results = await tusky_cmds_storage._destroy_storage_batches(
            client=client,
            walrus_pkg="0xpkg",
            storage_ids=["0xa", "0xb"],
            sender="0xsender",
            sponsor=None,
            mode="execute",
        )

        assert results == [executed]
        out = capsys.readouterr().out
        assert "MIST storage rebate value" in out
        assert "Total storage rebate redeemed: 2979200 MIST." in out

    async def test_simulate_mode_skips_rebate_summary(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            tusky_cmds_common, "SimulateTransaction", lambda **kwargs: object()
        )
        simulated = sui_prot.SimulateTransactionResponse(
            transaction=sui_prot.ExecutedTransaction(digest="0xdigest")
        )
        client = _FakeDestroyClient(responses=[SuiRpcResult(True, "", simulated)])

        results = await tusky_cmds_storage._destroy_storage_batches(
            client=client,
            walrus_pkg="0xpkg",
            storage_ids=["0xa"],
            sender="0xsender",
            sponsor=None,
            mode="simulate",
        )

        assert results == [simulated]
        out = capsys.readouterr().out
        assert "MIST" not in out

    async def test_zero_rebate_still_prints_total(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            tusky_cmds_common, "ExecuteTransaction", lambda **kwargs: object()
        )
        executed = sui_prot.ExecutedTransaction(
            digest="0xdigest",
            effects=sui_prot.TransactionEffects(
                gas_used=sui_prot.GasCostSummary(storage_rebate=0)
            ),
        )
        client = _FakeDestroyClient(responses=[SuiRpcResult(True, "", executed)])

        results = await tusky_cmds_storage._destroy_storage_batches(
            client=client,
            walrus_pkg="0xpkg",
            storage_ids=["0xa"],
            sender="0xsender",
            sponsor=None,
            mode="execute",
        )

        assert results == [executed]
        out = capsys.readouterr().out
        assert "Total storage rebate redeemed: 0 MIST." in out

    async def test_batches_storage_ids_at_max_per_ptb(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            tusky_cmds_common, "ExecuteTransaction", lambda **kwargs: object()
        )
        storage_ids = [
            f"0x{i:064x}" for i in range(tusky_cmds_storage._MAX_STORAGE_OPS_PER_PTB + 1)
        ]
        client = _FakeDestroyClient(
            responses=[
                SuiRpcResult(True, "", sui_prot.ExecutedTransaction(digest="0xdigest0")),
                SuiRpcResult(True, "", sui_prot.ExecutedTransaction(digest="0xdigest1")),
            ]
        )

        results = await tusky_cmds_storage._destroy_storage_batches(
            client=client,
            walrus_pkg="0xpkg",
            storage_ids=storage_ids,
            sender="0xsender",
            sponsor=None,
            mode="execute",
        )

        assert len(results) == 2
        assert len(client.txns) == 2
        assert len(client.txns[0].move_calls) == tusky_cmds_storage._MAX_STORAGE_OPS_PER_PTB
        assert len(client.txns[1].move_calls) == 1

    async def test_submission_failure_exits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            tusky_cmds_common, "ExecuteTransaction", lambda **kwargs: object()
        )
        client = _FakeDestroyClient(responses=[SuiRpcResult(False, "boom", None)])

        with pytest.raises(SystemExit):
            await tusky_cmds_storage._destroy_storage_batches(
                client=client,
                walrus_pkg="0xpkg",
                storage_ids=["0xa"],
                sender="0xsender",
                sponsor=None,
                mode="execute",
            )
