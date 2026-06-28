#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""pytest fixtures for pytusk integration tests. All integration tests run serially against testnet."""

import asyncio
import base64

import pytest
import pytest_asyncio
from pysui import (
    ExecuteTransaction,
    GetAddressCoinBalances,
    GetBasicCurrentEpochInfo,
    GetObject,
    GetObjectsOwnedByAddress,
)
from pysui.sui.sui_common.async_txn import AsyncSuiTransaction

from pytusk import BlobReceipt, PytuskConfiguration, QuiltReceipt, StoreBlob
from pytusk.client.walrus_client import WalrusClient

# Gas budget constants — update after running scratch/simulate_costs.py
REQUIRED_COIN_BUDGET: int = 0  # WAL units; TBD
REQUIRED_ADDR_BUDGET: int = 0  # SUI MIST units; TBD

_MIN_WAL: int = 1_000_000_000  # 1 WAL
_MIN_SUI: int = 1_000_000_000  # 1 SUI in MIST

_STORED_BLOB_DATA: bytes = b"pytusk integration test blob"
_STORED_BLOB_DATA_2: bytes = b"pytusk integration test blob 2"
_STORED_BLOB_EPOCHS: int = 2


async def _print_balances(client: WalrusClient, label: str = "") -> None:
    result = await client.execute_for_all(
        command=GetAddressCoinBalances(owner=client.pysui_client.config.active_address)
    )
    if not result.is_ok():
        print(f"  balances: check failed — {result.result_string}")
        return
    prefix = f"[{label}] " if label else ""
    for entry in result.result_data.balances:
        if not entry.coin_type:
            continue
        if "::sui::SUI" in entry.coin_type or "::wal::WAL" in entry.coin_type:
            token = entry.coin_type.split("::")[-1]
            print(f"  {prefix}{token}: {entry.balance}")


def _blob_id_from_u256(decimal_str: str) -> str:
    """Convert an on-chain u256 decimal blob_id to the base64url format used by the Walrus HTTP API."""
    n = int(decimal_str)
    return base64.urlsafe_b64encode(n.to_bytes(32, byteorder="little")).rstrip(b"=").decode()


def _end_epoch(obj) -> int:
    sv = obj.json.struct_value.fields.get("storage")
    if not sv or not sv.struct_value:
        return 0
    ev = sv.struct_value.fields.get("end_epoch")
    return int(ev.number_value) if ev else 0


async def _ensure_wal(client: WalrusClient) -> None:
    """Ensure the active address has at least 1 WAL and 1 SUI for integration tests.

    Rule A — WAL >= 1 and SUI >= 1: no action.
    Rule B/C — WAL < 1: SUI must be >= 2; swap 1_000_000_000 MIST to WAL.
    """
    result = await client.execute_for_all(
        command=GetAddressCoinBalances(owner=client.pysui_client.config.active_address)
    )
    if not result.is_ok():
        raise RuntimeError(f"Balance check failed: {result.result_string}")

    wal_balance: int = 0
    sui_balance: int = 0
    for entry in result.result_data.balances:
        if not entry.coin_type:
            continue
        if "::wal::WAL" in entry.coin_type:
            wal_balance = entry.balance or 0
        elif "::sui::SUI" in entry.coin_type:
            sui_balance = entry.balance or 0

    print(f"\n[ensure_wal] SUI={sui_balance} MIST  WAL={wal_balance}")
    if wal_balance >= _MIN_WAL and sui_balance >= _MIN_SUI:
        print("[ensure_wal] balances sufficient — no swap needed")
        return

    if wal_balance < _MIN_WAL:
        required_sui = _MIN_SUI * 2
        if sui_balance < required_sui:
            raise RuntimeError(
                f"Insufficient funds: WAL={wal_balance}, SUI={sui_balance} MIST. "
                f"Need {required_sui} MIST SUI (1 SUI buffer + 1 SUI to swap for WAL)."
            )
        exchange_obj_id = client.config.network.exchange_objects[0]
        sys_result = await client.execute(
            command=GetObject(object_id=client.config.network.system_object)
        )
        if not sys_result.is_ok():
            raise RuntimeError(f"Cannot get System object: {sys_result.result_string}")
        walrus_pkg = sys_result.result_data.json.struct_value.fields["package_id"].string_value

        txn: AsyncSuiTransaction = await client.transaction()
        split = await txn.split_coin(coin=txn.gas, amounts=[_MIN_SUI])
        wal_coin = await txn.move_call(
            target=f"{walrus_pkg}::wal_exchange::exchange_all_for_wal",
            arguments=[exchange_obj_id, split],
            type_arguments=[],
        )
        await txn.transfer_objects(
            transfers=[wal_coin],  # type: ignore
            recipient=client.pysui_client.config.active_address,
        )
        txdict = await txn.build_and_sign()
        tx_result = await client.execute(command=ExecuteTransaction(**txdict))
        if not tx_result.is_ok():
            raise RuntimeError(f"WAL exchange failed: {tx_result.result_string}")
        status = tx_result.result_data.effects.status
        if not (status and status.success):
            desc = status.error.description if status and status.error else "unknown error"
            raise RuntimeError(f"WAL exchange transaction aborted: {desc}")
        gas = tx_result.result_data.effects.gas_used
        print(f"[ensure_wal] swap PTB OK — gas={gas}")
        await asyncio.sleep(5)
        await _print_balances(client, label="ensure_wal post-swap")
    else:
        raise RuntimeError(
            f"Insufficient SUI: have {sui_balance} MIST, need {_MIN_SUI} MIST."
        )


async def _cleanup_blobs(client: WalrusClient, session_blob_ids: list[str] | None = None) -> None:
    """Delete active deletable blobs and burn expired blobs owned by the active address."""
    epoch_result = await client.execute(command=GetBasicCurrentEpochInfo())
    if not epoch_result.is_ok():
        print(f"\ncleanup: cannot get epoch — {epoch_result.result_string}")
        return
    current_epoch = epoch_result.result_data.epoch

    active_deletable = []
    expired = []
    seen_ids: set[str] = set()

    def _classify(obj) -> None:
        if not (obj.object_type and "::blob::Blob" in obj.object_type):
            return
        oid = getattr(obj, "object_id", None)
        if oid and oid in seen_ids:
            return
        if oid:
            seen_ids.add(oid)
        if not (obj.json and obj.json.struct_value):
            return
        fields = obj.json.struct_value.fields
        storage_val = fields.get("storage")
        if not (storage_val and storage_val.struct_value):
            return
        end_epoch_val = storage_val.struct_value.fields.get("end_epoch")
        end_epoch = int(end_epoch_val.number_value) if end_epoch_val else 0
        if end_epoch <= current_epoch:
            expired.append(obj)
        else:
            deletable_val = fields.get("deletable")
            if deletable_val and deletable_val.bool_value:
                active_deletable.append(obj)

    for obj_id in (session_blob_ids or []):
        if not obj_id:
            continue
        obj_result = await client.execute(command=GetObject(object_id=obj_id))
        if obj_result.is_ok():
            _classify(obj_result.result_data)

    objects_result = await client.execute_for_all(
        command=GetObjectsOwnedByAddress(owner=client.pysui_client.config.active_address)
    )
    if objects_result.is_ok():
        for obj in objects_result.result_data.objects:
            _classify(obj)
    else:
        print(f"\ncleanup: cannot list objects — {objects_result.result_string}")

    if not active_deletable and not expired:
        print("\ncleanup: no blobs to clean up")
        return

    sys_result = await client.execute(
        command=GetObject(object_id=client.config.network.system_object)
    )
    if not sys_result.is_ok():
        print(f"\ncleanup: cannot get System object — {sys_result.result_string}")
        return
    walrus_pkg = sys_result.result_data.json.struct_value.fields["package_id"].string_value

    if active_deletable:
        try:
            txn: AsyncSuiTransaction = await client.transaction()
            storage_objects = []
            for blob_obj in active_deletable:
                storage = await txn.move_call(
                    target=f"{walrus_pkg}::system::delete_blob",
                    arguments=[client.config.network.system_object, blob_obj.object_id],
                    type_arguments=[],
                )
                storage_objects.append(storage)
            await txn.transfer_objects(
                transfers=storage_objects,  # type: ignore
                recipient=client.pysui_client.config.active_address,
            )
            txdict = await txn.build_and_sign()
            result = await client.execute(command=ExecuteTransaction(**txdict))
            if result.is_ok():
                print(f"\ncleanup: deleted {len(active_deletable)} active deletable blob(s)")
                await _print_balances(client, label="cleanup post-delete")
            else:
                print(f"\ncleanup: delete failed — {result.result_string}")
        except Exception as exc:
            print(f"\ncleanup: delete PTB failed (blobs will clean up next session) — {exc}")

    if expired:
        for batch_start in range(0, len(expired), 100):
            batch = expired[batch_start:batch_start + 100]
            try:
                txn: AsyncSuiTransaction = await client.transaction()
                for blob_obj in batch:
                    await txn.move_call(
                        target=f"{walrus_pkg}::blob::burn",
                        arguments=[blob_obj.object_id],
                        type_arguments=[],
                    )
                txdict = await txn.build_and_sign()
                result = await client.execute(command=ExecuteTransaction(**txdict))
                if result.is_ok():
                    print(f"\ncleanup: burned {len(batch)} expired blob(s)")
                    await _print_balances(client, label="cleanup post-burn")
                else:
                    print(f"\ncleanup: burn failed — {result.result_string}")
            except Exception as exc:
                print(f"\ncleanup: burn PTB failed (blobs will expire naturally) — {exc}")


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def pytusk_config() -> PytuskConfiguration:
    return PytuskConfiguration()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def walrus_client(pytusk_config: PytuskConfiguration):
    async with WalrusClient(pytusk_config=pytusk_config) as client:
        _session_blob_ids: list[str] = []
        _orig_execute = client.execute

        async def _tracking_execute(
            *, command, timeout: float | None = None, headers: dict | None = None
        ):
            result = await _orig_execute(command=command, timeout=timeout, headers=headers)
            if result.is_ok() and isinstance(result.result_data, (BlobReceipt, QuiltReceipt)):
                oid = result.result_data.object_id
                if oid:
                    _session_blob_ids.append(oid)
            return result

        client.execute = _tracking_execute  # type: ignore[method-assign]

        await _ensure_wal(client)
        yield client
        await _cleanup_blobs(client, session_blob_ids=_session_blob_ids)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def stored_blob(walrus_client: WalrusClient):
    """Yield a known blob for read tests. Reuses an existing owned Blob object if one
    exists (saves WAL); falls back to storing a fresh blob.

    Yields:
        tuple[str, str, bytes | None]: (blob_id, object_id, data)
            data is None if an existing blob was reused (content unknown).
            data is bytes if a fresh blob was stored this session.
    """
    blob_id: str = ""
    object_id: str = ""
    data: bytes | None = None
    found = False

    epoch_result = await walrus_client.execute(command=GetBasicCurrentEpochInfo())
    current_epoch: int = epoch_result.result_data.epoch if epoch_result.is_ok() else 0

    result = await walrus_client.execute_for_all(
        command=GetObjectsOwnedByAddress(
            owner=walrus_client.pysui_client.config.active_address
        )
    )
    if result.is_ok():
        blobs = [
            obj
            for obj in result.result_data.objects
            if obj.object_type
            and "::blob::Blob" in obj.object_type
            and obj.json
            and obj.json.struct_value
            and obj.json.struct_value.fields.get("blob_id")
            and obj.json.struct_value.fields["blob_id"].string_value
            and _end_epoch(obj) > current_epoch
        ]
        if blobs:
            blobs.sort(key=_end_epoch, reverse=True)
            best = blobs[0]
            blob_id = _blob_id_from_u256(
                best.json.struct_value.fields["blob_id"].string_value
            )
            object_id = best.object_id
            found = True

    if not found:
        store_result = await walrus_client.execute(
            command=StoreBlob(
                data=_STORED_BLOB_DATA, epochs=_STORED_BLOB_EPOCHS, deletable=True
            )
        )
        if not store_result.is_ok():
            pytest.fail(f"stored_blob: StoreBlob failed — {store_result.result_string}")
        receipt = store_result.result_data
        blob_id = receipt.blob_id
        object_id = receipt.object_id
        data = _STORED_BLOB_DATA

    yield blob_id, object_id, data


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def stored_blob_2(walrus_client: WalrusClient):
    """Store a second distinct blob for tests requiring two different blobs."""
    store_result = await walrus_client.execute(
        command=StoreBlob(
            data=_STORED_BLOB_DATA_2, epochs=_STORED_BLOB_EPOCHS, deletable=True
        )
    )
    if not store_result.is_ok():
        pytest.fail(f"stored_blob_2: StoreBlob failed — {store_result.result_string}")
    receipt = store_result.result_data
    yield receipt.blob_id, receipt.object_id, _STORED_BLOB_DATA_2
