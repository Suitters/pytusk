#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""pytest fixtures for pytusk integration tests. All integration tests run serially against testnet."""

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

from pytusk import PytuskConfiguration, StoreBlob
from pytusk.client.walrus_client import WalrusClient

# Gas budget constants — update after running scratch/simulate_costs.py
REQUIRED_COIN_BUDGET: int = 0  # WAL units; TBD
REQUIRED_ADDR_BUDGET: int = 0  # SUI MIST units; TBD

_MIN_WAL: int = 500_000
_SWAP_SUI_AMOUNT: int = 1_000_000  # MIST

_STORED_BLOB_DATA: bytes = b"pytusk integration test blob"
_STORED_BLOB_EPOCHS: int = 2


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


async def _ensure_wal(client: WalrusClient, cfg: PytuskConfiguration) -> None:
    """Swap SUI->WAL if the active address holds less than _MIN_WAL."""
    result = await client.execute_for_all(
        command=GetAddressCoinBalances(
            owner=client.pysui_client.config.active_address
        )
    )
    if not result.is_ok():
        raise RuntimeError(f"Balance check failed: {result.result_string}")

    wal_balance: int = 0
    for entry in result.result_data.balances:
        if entry.coin_type and "::wal::WAL" in entry.coin_type:
            wal_balance = entry.balance or 0
            break

    if wal_balance >= _MIN_WAL:
        return

    network = cfg.active_network_entry
    exchange_obj_id = network.exchange_objects[0]

    obj_result = await client.execute(command=GetObject(object_id=exchange_obj_id))
    if not obj_result.is_ok():
        raise RuntimeError(f"Cannot get exchange object: {obj_result.result_string}")
    wal_exchange_pkg = obj_result.result_data.object_type.split("::")[0]

    txn: AsyncSuiTransaction = await client.transaction()
    split = await txn.split_coin(coin=txn.gas, amounts=[_SWAP_SUI_AMOUNT])
    wal_coin = await txn.move_call(
        target=f"{wal_exchange_pkg}::wal_exchange::exchange_all_for_wal",
        arguments=[exchange_obj_id, split],
        type_arguments=[],
    )
    await txn.transfer_objects(
        transfers=[wal_coin],
        recipient=client.pysui_client.config.active_address,  # type: ignore
    )
    txdict = await txn.build_and_sign()
    tx_result = await client.execute(command=ExecuteTransaction(**txdict))
    if not tx_result.is_ok():
        raise RuntimeError(f"WAL exchange failed: {tx_result.result_string}")
    status = tx_result.result_data.effects.status
    if not (status and status.success):
        desc = status.error.description if status and status.error else "unknown error"
        raise RuntimeError(f"WAL exchange transaction aborted: {desc}")


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def pytusk_config() -> PytuskConfiguration:
    return PytuskConfiguration()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def walrus_client(pytusk_config: PytuskConfiguration):
    async with WalrusClient(pytusk_config=pytusk_config) as client:
        await _ensure_wal(client, pytusk_config)
        yield client


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
