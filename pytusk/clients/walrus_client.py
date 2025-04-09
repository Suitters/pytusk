#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Pytusk Walrus clients."""

from pysui.sui.sui_clients.async_client import SuiClient
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
        self.sui_client = SuiClient(pysui_cfg)
