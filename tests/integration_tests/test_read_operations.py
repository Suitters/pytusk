#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Integration tests for Walrus read commands."""

import pytest

from pytusk import (
    ConcatBlobs,
    ReadBlob,
    ReadBlobByObjectId,
    ReadBlobPartial,
)
from pytusk.client.walrus_client import WalrusClient


class TestReadBlob:
    async def test_read_blob_returns_bytes(
        self, walrus_client: WalrusClient, stored_blob
    ):
        blob_id, _, _ = stored_blob
        result = await walrus_client.execute(command=ReadBlob(blob_id=blob_id))
        assert result.is_ok(), f"ReadBlob failed: {result.result_string}"
        assert isinstance(result.result_data.content, bytes)
        assert len(result.result_data.content) > 0

    async def test_read_blob_content_matches_when_known(
        self, walrus_client: WalrusClient, stored_blob
    ):
        blob_id, _, data = stored_blob
        if data is None:
            pytest.skip("stored_blob reused an existing blob — content unknown")
        result = await walrus_client.execute(command=ReadBlob(blob_id=blob_id))
        assert result.is_ok()
        assert result.result_data.content == data

    async def test_read_blob_not_found(self, walrus_client: WalrusClient):
        result = await walrus_client.execute(
            command=ReadBlob(blob_id="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        )
        assert not result.is_ok()


class TestReadBlobPartial:
    async def test_read_partial_returns_bytes(
        self, walrus_client: WalrusClient, stored_blob
    ):
        blob_id, _, _ = stored_blob
        result = await walrus_client.execute(
            command=ReadBlobPartial(blob_id=blob_id, start=0, length=5)
        )
        assert result.is_ok(), f"ReadBlobPartial failed: {result.result_string}"
        assert isinstance(result.result_data.content, bytes)
        assert len(result.result_data.content) == 5

    async def test_read_partial_slice_correct(
        self, walrus_client: WalrusClient, stored_blob
    ):
        blob_id, _, data = stored_blob
        if data is None:
            pytest.skip("stored_blob reused an existing blob — content unknown")
        result = await walrus_client.execute(
            command=ReadBlobPartial(blob_id=blob_id, start=0, length=5)
        )
        assert result.is_ok()
        assert result.result_data.content == data[:5]


class TestReadBlobByObjectId:
    async def test_read_by_object_id(self, walrus_client: WalrusClient, stored_blob):
        _, object_id, _ = stored_blob
        if not object_id:
            pytest.skip("stored_blob has no object_id (alreadyCertified path)")
        result = await walrus_client.execute(
            command=ReadBlobByObjectId(object_id=object_id)
        )
        assert result.is_ok(), f"ReadBlobByObjectId failed: {result.result_string}"
        assert isinstance(result.result_data.content, bytes)
        assert len(result.result_data.content) > 0


class TestConcatBlobs:
    async def test_concat_blob_with_itself(
        self, walrus_client: WalrusClient, stored_blob
    ):
        blob_id, _, data = stored_blob
        result = await walrus_client.execute(
            command=ConcatBlobs(ids=[blob_id, blob_id])
        )
        assert result.is_ok(), f"ConcatBlobs failed: {result.result_string}"
        content = result.result_data.content
        assert isinstance(content, bytes)
        if data is not None:
            assert content == data + data
        else:
            assert len(content) > 0
