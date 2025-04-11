#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Pytusk Walrus clients."""

from typing import Optional
from json import JSONDecodeError
import httpx
from pysui.sui.sui_clients.async_client import SuiClient, SuiRpcResult
from pysui.sui.sui_config import SuiConfig

from pytusk.config.tusk_config import PytuskConfiguration


class ClientAsync:
    """Asyncronous Client"""

    def __init__(self, *, pytusk_config: PytuskConfiguration):
        """Initializer"""

        self.config = pytusk_config
        # Initialize underlying Pysui Async JSON RPC
        tgroup = pytusk_config.pysui_config.model.active_group
        akp = tgroup.keypair_for_address(address=tgroup.active_address).serialize()
        pysui_cfg = SuiConfig.user_config(
            rpc_url=tgroup.active_profile.url,
            prv_keys=[akp],
        )
        self._sui_client = SuiClient(pysui_cfg)
        self._httpx = self._sui_client._client

    @property
    def sui_client(self) -> SuiClient:
        """Get underlying pysui client."""
        return self._sui_client

    async def get_blob(self, *, blob_id) -> bytes:
        """Get's a blob by ID."""
        url = f"{self.config.aggregator_url}/v1/blobs/{blob_id}"
        try:
            result = await self._httpx.get(url)
            if result.is_error:
                return SuiRpcResult(False, "ERROR", result.content.decode("utf-8"))
            return SuiRpcResult(True, "", result_data=result.content)
        except (
            httpx.HTTPError,
            httpx.InvalidURL,
            httpx.CookieConflict,
        ) as hexc:
            return SuiRpcResult(
                False, f"HTTPX error: {hexc.__class__.__name__} -> {vars(hexc)}"
            )

    async def put_blob(
        self,
        *,
        data: bytes,
        deletable: Optional[bool] = False,
        sends_to: Optional[str] = None,
    ) -> dict:
        """Get's a blob by ID."""
        url = f"{self.config.publisher_url}/v1/blobs"
        headers = {"Content-Type": "application/octet-stream"}
        params = {}
        # if encoding_type is not None:
        #     params["encoding_type"] = encoding_type
        # if epochs is not None:
        #     params["epochs"] = str(epochs)
        if deletable is not None:
            params["deletable"] = "true" if deletable else "false"
        if sends_to is not None:
            params["send_object_to"] = sends_to
        try:
            result = await self._httpx.put(
                url, data=data, params=params, headers=headers
            )
            return result.json()
        except JSONDecodeError as jexc:
            raise ValueError(f"JSON Decoder Error: {jexc.msg}") from jexc
        except (
            httpx.HTTPError,
            httpx.InvalidURL,
            httpx.CookieConflict,
        ) as hexc:
            raise ValueError(
                f"HTTPX error: {hexc.__class__.__name__} -> {vars(hexc)}"
            ) from hexc
