#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Pysui Configuration Wrapper."""

import os
from pathlib import Path
from typing import Optional
from pysui import PysuiConfiguration

from pytusk.config.walrus_config import load_from_yaml, WalrusConfig


class PytuskConfiguration:
    """Wrapper for PysuiConfiguration."""

    _WALRUS_DEFAULT_CFG: str = "~/.config/walrus/client_config.yaml"
    _TUSK_JSON_GROUP: str = "walrus_rpc_group"

    _TESTNET_WALRUS: dict[str, str] = {
        "testnet_publisher": "https://publisher.walrus-testnet.walrus.space",
        "testnet_aggregator": "https://aggregator.walrus-testnet.walrus.space",
    }

    def __init__(
        self,
        *,
        pysui_config: PysuiConfiguration,
        walrus_config_path: Optional[str] = None,
    ):
        """."""
        self._pysui_config: PysuiConfiguration = pysui_config
        # Get the walrus config
        self._walrus_cfg_path = Path(
            os.path.expanduser(walrus_config_path or self._WALRUS_DEFAULT_CFG)
        )
        self._walrus_config: WalrusConfig = load_from_yaml(
            self._walrus_cfg_path, pysui_config
        )
        self._aggregator: str = None
        self._publisher: str = None

    @property
    def publisher_url(self) -> str:
        """."""
        return "https://publisher.walrus-testnet.walrus.space"

    @property
    def aggregator_url(self) -> str:
        """."""
        return "https://aggregator.walrus-testnet.walrus.space"

    @property
    def walrus_config(self) -> WalrusConfig:
        """Fetch walrus config."""
        return self._walrus_config

    @property
    def pysui_config(self) -> PysuiConfiguration:
        """Fetch pysui config."""
        return self._pysui_config
