#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Integration tests for Walrus write commands."""

import asyncio
import dataclasses
import json

import pytest
from pysui import GetAddressCoinBalances

from pytusk import (
    BlobReceipt,
    PytuskConfiguration,
    QuiltReceipt,
    StoreBlob,
    StoreQuilt,
)
from pytusk.client.walrus_client import WalrusClient

_TEST_PAYLOAD: bytes = b"pytusk write test"
_QUILT_FILES: dict[str, bytes] = {
    "alpha": b"pytusk quilt patch alpha",
    "beta": b"pytusk quilt patch beta",
}


async def _balances(client: WalrusClient, label: str = "") -> None:
    r = await client.execute_for_all(
        command=GetAddressCoinBalances(owner=client.pysui_client.config.active_address)
    )
    if r.is_ok():
        prefix = f"[{label}] " if label else ""
        for e in r.result_data.balances:
            if e.coin_type and ("::sui::SUI" in e.coin_type or "::wal::WAL" in e.coin_type):
                print(f"  {prefix}{e.coin_type.split('::')[-1]}: {e.balance}")


class TestStoreBlob:
    async def test_store_receipt_fields(self, walrus_client: WalrusClient):
        result = await walrus_client.execute(
            command=StoreBlob(data=_TEST_PAYLOAD, epochs=1, deletable=True)
        )
        assert result.is_ok(), f"StoreBlob failed: {result.result_string}"
        receipt = result.result_data
        print(f"\n[StoreBlob permanent]\n{json.dumps(dataclasses.asdict(receipt), indent=2)}")
        await asyncio.sleep(5)
        await _balances(walrus_client, label="after store permanent")
        assert isinstance(receipt, BlobReceipt)
        assert receipt.blob_id
        assert receipt.expiry_epoch > 0

    async def test_store_deletable_flag(self, walrus_client: WalrusClient):
        result = await walrus_client.execute(
            command=StoreBlob(data=_TEST_PAYLOAD, epochs=1, deletable=True)
        )
        assert result.is_ok(), f"StoreBlob failed: {result.result_string}"
        receipt = result.result_data
        print(f"\n[StoreBlob deletable]\n{json.dumps(dataclasses.asdict(receipt), indent=2)}")
        await asyncio.sleep(5)
        await _balances(walrus_client, label="after store deletable")
        assert receipt.blob_id
        assert receipt.deletable is True

    async def test_duplicate_content_reuses_blob_id(self, walrus_client: WalrusClient):
        """Storing the same content twice returns the same blob_id (content addressing)."""
        payload = b"pytusk duplicate blob test"
        first = await walrus_client.execute(
            command=StoreBlob(data=payload, epochs=1, deletable=True)
        )
        assert first.is_ok()
        await asyncio.sleep(5)
        second = await walrus_client.execute(
            command=StoreBlob(data=payload, epochs=1, deletable=True)
        )
        assert second.is_ok()
        print(f"\n[StoreBlob duplicate]\n{json.dumps(dataclasses.asdict(second.result_data), indent=2)}")
        await asyncio.sleep(5)
        await _balances(walrus_client, label="after duplicate store")
        assert second.result_data.blob_id == first.result_data.blob_id


class TestStoreQuilt:
    async def test_store_quilt_receipt_fields(self, walrus_client: WalrusClient):
        result = await walrus_client.execute(
            command=StoreQuilt(files=_QUILT_FILES, epochs=1)
        )
        assert result.is_ok(), f"StoreQuilt failed: {result.result_string}"
        receipt = result.result_data
        print(f"\n[StoreQuilt]\n{json.dumps(dataclasses.asdict(receipt), indent=2)}")
        await asyncio.sleep(5)
        await _balances(walrus_client, label="after store quilt")
        assert isinstance(receipt, QuiltReceipt)
        assert receipt.quilt_id
        assert set(receipt.patch_keys) == set(_QUILT_FILES.keys())
        assert receipt.expiry_epoch > 0


class TestPublisherGuard:
    async def test_store_raises_without_publisher(self):
        cfg = PytuskConfiguration()
        cfg.active_network_entry.walrus_publisher_url = ""
        async with WalrusClient(pytusk_config=cfg) as client:
            with pytest.raises(ValueError, match="No publisher URL"):
                await client.execute(
                    command=StoreBlob(data=b"test", epochs=1, deletable=False)
                )
