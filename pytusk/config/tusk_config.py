#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Pytusk configuration."""

import dataclasses
import shutil
from pathlib import Path
from typing import Any

from dataclasses_json import DataClassJsonMixin

_DEFAULT_CONFIG_DIR: str = "~/.pytusk"
_CONFIG_FILENAME: str = "PytuskConfig.json"
_SUPPORTED_NETWORKS: frozenset[str] = frozenset({"testnet", "mainnet"})

_DEFAULT_NETWORKS: list[dict[str, Any]] = [
    {
        "network_name": "testnet",
        "pysui_group_name": "sui_gql_config",
        "pysui_profile_name": "testnet",
        "walrus_aggregator": "https://aggregator.walrus-testnet.walrus.space",
        "walrus_publisher": "https://publisher.walrus-testnet.walrus.space",
        "system_object": "0x6c2547cbbc38025cf3adac45f63cb0a8d12ecf777cdc75a4971612bf97fdf6af",
        "staking_object": "0xbe46180321c30aab2f8b3501e24048377287fa708018a5b7c2792b35fe339ee3",
        "exchange_objects": [
            "0xf4d164ea2def5fe07dc573992a029e010dba09b1a8dcbc44c5c2e79567f39073",
            "0x19825121c52080bb1073662231cfea5c0e4d905fd13e95f21e9a018f2ef41862",
            "0x83b454e524c71f30803f4d6c302a86fb6a39e96cdfb873c2d1e93bc1c26a3bc5",
            "0x8d63209cf8589ce7aef8f262437163c67577ed09f3e636a9d8e0813843fb8bf1",
        ],
    },
    {
        "network_name": "mainnet",
        "pysui_group_name": "sui_gql_config",
        "pysui_profile_name": "mainnet",
        "walrus_aggregator": "https://aggregator.walrus-mainnet.walrus.space",
        "walrus_publisher": "https://upload-relay.mainnet.walrus.space",
        "system_object": "0x2134d52768ea07e8c43570ef975eb3e4c27a39fa6396bef985b5abc58d03ddd2",
        "staking_object": "0x10b9d30c28448939ce6c4d6c6e0ffce4a7f8a4ada8248bdad09ef8b70e4a3904",
        "exchange_objects": [],
    },
]


@dataclasses.dataclass
class WalrusNetworkConfig(DataClassJsonMixin):
    """Per-network Walrus and pysui configuration.

    Args:
        network_name (str): Network identifier (e.g. 'testnet', 'mainnet').
        pysui_group_name (str): pysui ProfileGroup name (e.g. 'sui_gql_config').
        pysui_profile_name (str): pysui Profile name matching the user's pysui config.
        walrus_aggregator (str): Walrus aggregator URL for read operations.
        walrus_publisher (str): Walrus publisher (or upload relay) URL for write operations.
        system_object (str): Walrus system object ID on-chain.
        staking_object (str): Walrus staking object ID on-chain.
        exchange_objects (list[str]): Walrus exchange object IDs for SUI->WAL swap (testnet only).
    """

    network_name: str = dataclasses.field(default="")
    pysui_group_name: str = dataclasses.field(default="")
    pysui_profile_name: str = dataclasses.field(default="")
    walrus_aggregator: str = dataclasses.field(default="")
    walrus_publisher: str = dataclasses.field(default="")
    system_object: str = dataclasses.field(default="")
    staking_object: str = dataclasses.field(default="")
    exchange_objects: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class PytuskConfigModel(DataClassJsonMixin):
    """Serialisable pytusk configuration state.

    Args:
        version (str): Config schema version.
        pysui_config_path (str): Path to the pysui config folder.
        walrus_binary_path (str | None): Path to the walrus binary, or None if not found.
        active_network (str): Currently active network name.
        networks (list[WalrusNetworkConfig]): Per-network configuration entries.
    """

    version: str = dataclasses.field(default="1.0.0")
    pysui_config_path: str = dataclasses.field(
        default_factory=lambda: str(Path("~/.pysui").expanduser())
    )
    walrus_binary_path: str | None = dataclasses.field(default=None)
    active_network: str = dataclasses.field(default="testnet")
    networks: list[WalrusNetworkConfig] = dataclasses.field(default_factory=list)


class PytuskConfiguration:
    """pytusk configuration.

    Loads from or creates a JSON config file (default: ~/.pytusk/PytuskConfig.json).
    On first run the file is auto-created with testnet and mainnet defaults pre-populated.
    The walrus binary path is auto-detected from ~/.cargo/bin/walrus or PATH on creation.

    Args:
        from_cfg_path (str | None): Directory containing PytuskConfig.json.
            Defaults to ~/.pytusk/.
        active_network (str | None): Override the active network ('testnet' or 'mainnet').
        persist (bool): If True, persist any overrides back to the config file.
    """

    _model: PytuskConfigModel
    _cfg_dir: Path

    def __init__(
        self,
        *,
        from_cfg_path: str | None = None,
        active_network: str | None = None,
        persist: bool = False,
    ) -> None:
        """Initialise PytuskConfiguration.

        Loads from PytuskConfig.json if it exists; auto-creates with defaults on first run.

        Args:
            from_cfg_path (str | None): Directory containing PytuskConfig.json.
                Defaults to ~/.pytusk/.
            active_network (str | None): Override the active network. Must be
                'testnet' or 'mainnet'.
            persist (bool): If True, persist any overrides back to the config file.

        Raises:
            ValueError: If active_network is not a supported network.
        """
        cfg_dir = Path(from_cfg_path or _DEFAULT_CONFIG_DIR).expanduser()
        cfg_file = cfg_dir / _CONFIG_FILENAME

        if cfg_file.exists():
            self._model = PytuskConfigModel.from_json(
                cfg_file.read_text(encoding="utf-8")
            )
        else:
            self._model = self._default_model()
            self._write_model(cfg_dir)

        if active_network is not None:
            if active_network not in _SUPPORTED_NETWORKS:
                raise ValueError(
                    f"Unsupported network '{active_network}'. "
                    f"Must be one of {sorted(_SUPPORTED_NETWORKS)}."
                )
            self._model.active_network = active_network

        self._cfg_dir = cfg_dir

        if persist:
            self._write_model(cfg_dir)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_walrus_binary() -> str | None:
        """Return the walrus binary path, or None if not found."""
        cargo_path = Path("~/.cargo/bin/walrus").expanduser()
        if cargo_path.exists():
            return str(cargo_path)
        return shutil.which("walrus")

    @staticmethod
    def _default_model() -> PytuskConfigModel:
        """Build a PytuskConfigModel with built-in testnet/mainnet defaults."""
        return PytuskConfigModel(
            walrus_binary_path=PytuskConfiguration._detect_walrus_binary(),
            networks=[
                WalrusNetworkConfig.from_dict(entry) for entry in _DEFAULT_NETWORKS
            ],
        )

    def _write_model(self, cfg_dir: Path) -> None:
        """Serialise the model to PytuskConfig.json in cfg_dir."""
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / _CONFIG_FILENAME).write_text(
            self._model.to_json(indent=2), encoding="utf-8"
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def version(self) -> str:
        """Config schema version."""
        return self._model.version

    @property
    def pysui_config_path(self) -> str:
        """Path to the pysui config folder."""
        return self._model.pysui_config_path

    @property
    def walrus_binary_path(self) -> str | None:
        """Path to the walrus binary, or None."""
        return self._model.walrus_binary_path

    @property
    def active_network(self) -> str:
        """Currently active network name."""
        return self._model.active_network

    @property
    def networks(self) -> list[WalrusNetworkConfig]:
        """All network configuration entries."""
        return self._model.networks

    @property
    def active_network_entry(self) -> WalrusNetworkConfig:
        """WalrusNetworkConfig for the active network.

        Returns:
            WalrusNetworkConfig: Active network configuration.

        Raises:
            ValueError: If the active network has no matching entry.
        """
        for net in self._model.networks:
            if net.network_name == self._model.active_network:
                return net
        raise ValueError(
            f"No network config found for active network '{self._model.active_network}'."
        )

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def set_active_network(self, network: str, *, persist: bool = False) -> None:
        """Set the active network.

        Args:
            network (str): Network name ('testnet' or 'mainnet').
            persist (bool): If True, persist the change to the config file.

        Raises:
            ValueError: If network is not a supported network.
        """
        if network not in _SUPPORTED_NETWORKS:
            raise ValueError(
                f"Unsupported network '{network}'. "
                f"Must be one of {sorted(_SUPPORTED_NETWORKS)}."
            )
        self._model.active_network = network
        if persist:
            self._write_model(self._cfg_dir)

    def set_pysui_config_path(self, path: str, *, persist: bool = False) -> None:
        """Set the pysui config folder path.

        Args:
            path (str): Filesystem path to the pysui config folder.
            persist (bool): If True, persist the change to the config file.
        """
        self._model.pysui_config_path = str(Path(path).expanduser())
        if persist:
            self._write_model(self._cfg_dir)

    def set_walrus_binary_path(self, path: str, *, persist: bool = False) -> None:
        """Set the walrus binary path.

        Args:
            path (str): Filesystem path to the walrus binary.
            persist (bool): If True, persist the change to the config file.
        """
        self._model.walrus_binary_path = str(Path(path).expanduser())
        if persist:
            self._write_model(self._cfg_dir)

    def save(self) -> None:
        """Persist the current configuration to disk."""
        self._write_model(self._cfg_dir)

    def save_to(self, path: str) -> None:
        """Persist the current configuration to an alternate directory.

        Args:
            path (str): Directory path to write PytuskConfig.json into.
        """
        self._write_model(Path(path).expanduser())
