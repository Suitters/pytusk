#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Integration tests for Walrus write commands."""

import pytest

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


class TestStoreBlob:
    async def test_store_receipt_fields(self, walrus_client: WalrusClient):
        result = await walrus_client.execute(
            command=StoreBlob(data=_TEST_PAYLOAD, epochs=1, deletable=False)
        )
        assert result.is_ok(), f"StoreBlob failed: {result.result_string}"
        receipt = result.result_data
        assert isinstance(receipt, BlobReceipt)
        assert receipt.blob_id
        assert receipt.expiry_epoch > 0

    async def test_store_deletable_flag(self, walrus_client: WalrusClient):
        result = await walrus_client.execute(
            command=StoreBlob(data=_TEST_PAYLOAD, epochs=1, deletable=True)
        )
        assert result.is_ok(), f"StoreBlob failed: {result.result_string}"
        receipt = result.result_data
        assert receipt.blob_id
        assert receipt.deletable is True

    async def test_already_certified_cost_zero(self, walrus_client: WalrusClient):
        """Storing the same content twice returns alreadyCertified with cost=0."""
        payload = b"pytusk duplicate blob test"
        first = await walrus_client.execute(
            command=StoreBlob(data=payload, epochs=1, deletable=True)
        )
        assert first.is_ok()
        second = await walrus_client.execute(
            command=StoreBlob(data=payload, epochs=1, deletable=True)
        )
        assert second.is_ok()
        assert second.result_data.blob_id == first.result_data.blob_id
        assert second.result_data.cost == 0


class TestStoreQuilt:
    async def test_store_quilt_receipt_fields(self, walrus_client: WalrusClient):
        result = await walrus_client.execute(
            command=StoreQuilt(files=_QUILT_FILES, epochs=1)
        )
        assert result.is_ok(), f"StoreQuilt failed: {result.result_string}"
        receipt = result.result_data
        assert isinstance(receipt, QuiltReceipt)
        assert receipt.quilt_id
        assert set(receipt.patch_keys) == set(_QUILT_FILES.keys())
        assert receipt.expiry_epoch > 0


class TestPublisherGuard:
    async def test_store_raises_without_publisher(self):
        cfg = PytuskConfiguration()
        cfg.active_network_entry.walrus_publisher = ""
        async with WalrusClient(pytusk_config=cfg) as client:
            with pytest.raises(ValueError, match="No publisher URL"):
                await client.execute(
                    command=StoreBlob(data=b"test", epochs=1, deletable=False)
                )
