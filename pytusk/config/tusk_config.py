#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Pytusk configuration."""

import dataclasses
import enum
import shutil
from pathlib import Path
from typing import Any

from dataclasses_json import DataClassJsonMixin
from pysui import PysuiConfiguration


class NetworkType(str, enum.Enum):
    """Indicates whether a network configuration targets a test or production environment."""

    __str__ = str.__str__

    TEST = "test"
    PRODUCTION = "production"


_RESERVED_NETWORKS: frozenset[str] = frozenset({"testnet", "mainnet"})
_DEFAULT_CONFIG_DIR: str = "~/.pysui"
_CONFIG_FILENAME: str = "PytuskConfig.json"

_CONFIG_VERSION: str = "1.1.0"

_MYSTEN_RELAY_NAME: str = "mysten"

_DEFAULT_RELAYS: dict[str, list[dict[str, str]]] = {
    "testnet": [
        {
            "relay_name": _MYSTEN_RELAY_NAME,
            "relay_url": "https://upload-relay.testnet.walrus.space",
        }
    ],
    "mainnet": [
        {
            "relay_name": _MYSTEN_RELAY_NAME,
            "relay_url": "https://upload-relay.mainnet.walrus.space",
        }
    ],
}

_DEFAULT_NETWORKS: list[dict[str, Any]] = [
    {
        "network_name": "testnet",
        "pysui_group_name": "sui_gql_config",
        "pysui_profile_name": "testnet",
        "walrus_aggregator_url": "https://aggregator.walrus-testnet.walrus.space",
        "walrus_publisher_url": "https://publisher.walrus-testnet.walrus.space",
        "system_object": "0x6c2547cbbc38025cf3adac45f63cb0a8d12ecf777cdc75a4971612bf97fdf6af",
        "staking_object": "0xbe46180321c30aab2f8b3501e24048377287fa708018a5b7c2792b35fe339ee3",
        "exchange_objects": [
            "0xf4d164ea2def5fe07dc573992a029e010dba09b1a8dcbc44c5c2e79567f39073",
            "0x19825121c52080bb1073662231cfea5c0e4d905fd13e95f21e9a018f2ef41862",
            "0x83b454e524c71f30803f4d6c302a86fb6a39e96cdfb873c2d1e93bc1c26a3bc5",
            "0x8d63209cf8589ce7aef8f262437163c67577ed09f3e636a9d8e0813843fb8bf1",
        ],
        "network_type": "test",
    },
    {
        "network_name": "mainnet",
        "pysui_group_name": "sui_gql_config",
        "pysui_profile_name": "mainnet",
        "walrus_aggregator_url": "https://aggregator.walrus-mainnet.walrus.space",
        "walrus_publisher_url": "",
        "system_object": "0x2134d52768ea07e8c43570ef975eb3e4c27a39fa6396bef985b5abc58d03ddd2",
        "staking_object": "0x10b9d30c28448939ce6c4d6c6e0ffce4a7f8a4ada8248bdad09ef8b70e4a3904",
        "exchange_objects": [],
        "wal_coin_type": "0x356a26eb9e012a68958082340d4c4116e7f55615cf27affcff209cf0ae544f59::wal::WAL",
        "network_type": "production",
    },
]


@dataclasses.dataclass
class RelayConfig(DataClassJsonMixin):
    """A named Walrus upload relay endpoint.

    Relay entries carry no network field: a relay is associated with a
    network structurally, by living in that network's ``relays`` list.

    Args:
        relay_name (str): Unique name for this relay within its network.
        relay_url (str): Base URL of the upload relay service.
    """

    relay_name: str = dataclasses.field(default="")
    relay_url: str = dataclasses.field(default="")

    def __post_init__(self) -> None:
        if not self.relay_name:
            raise ValueError("RelayConfig.relay_name must not be empty.")


@dataclasses.dataclass
class WalrusNetworkConfig(DataClassJsonMixin):
    """Per-network Walrus and pysui configuration.

    Args:
        network_name (str): Network identifier (e.g. 'testnet', 'mainnet').
        pysui_group_name (str): pysui ProfileGroup name (e.g. 'sui_gql_config').
        pysui_profile_name (str): pysui Profile name matching the user's pysui config.
        walrus_aggregator_url (str): Walrus aggregator URL for read operations.
        walrus_publisher_url (str): Walrus publisher URL for write operations. May be
            empty (e.g. mainnet has no public unauthenticated publisher by design).
        system_object (str): Walrus system object ID on-chain.
        staking_object (str): Walrus staking object ID on-chain.
        exchange_objects (list[str]): Walrus exchange object IDs for SUI->WAL swap (testnet only).
        relays (list[RelayConfig]): Named upload relay endpoints available on
            this network. Empty on user-defined networks, which ship no
            built-in relay.
        active_relay (str | None): Name of the relay used when a caller does
            not name one explicitly. None when the network has no default.
        wal_coin_type (str): Canonical `<package>::wal::WAL` coin type for
            this network, used for exact WAL-coin identification instead of
            a substring match. Empty ("") on networks (e.g. testnet) whose
            WAL package address isn't stable enough to pin — callers fall
            back to a substring match in that case.
        network_type (NetworkType): Whether this network is TEST or PRODUCTION. Defaults to TEST.
    """

    network_name: str = dataclasses.field(default="")
    pysui_group_name: str = dataclasses.field(default="")
    pysui_profile_name: str = dataclasses.field(default="")
    walrus_aggregator_url: str = dataclasses.field(default="")
    walrus_publisher_url: str = dataclasses.field(default="")
    system_object: str = dataclasses.field(default="")
    staking_object: str = dataclasses.field(default="")
    exchange_objects: list[str] = dataclasses.field(default_factory=list)
    relays: list[RelayConfig] = dataclasses.field(default_factory=list)
    active_relay: str | None = dataclasses.field(default=None)
    wal_coin_type: str = dataclasses.field(default="")
    network_type: NetworkType = dataclasses.field(default=NetworkType.TEST)

    def __post_init__(self) -> None:
        if not self.network_name:
            raise ValueError("WalrusNetworkConfig.network_name must not be empty.")


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

    version: str = dataclasses.field(default=_CONFIG_VERSION)
    pysui_config_path: str = dataclasses.field(
        default_factory=lambda: str(Path("~/.pysui").expanduser())
    )
    walrus_binary_path: str | None = dataclasses.field(default=None)
    active_network: str = dataclasses.field(default="testnet")
    networks: list[WalrusNetworkConfig] = dataclasses.field(default_factory=list)


class PytuskConfiguration:
    """pytusk configuration.

    Loads from or creates a JSON config file (default: ~/.pysui/PytuskConfig.json).
    On first run the file is auto-created with testnet and mainnet defaults pre-populated.
    The walrus binary path is auto-detected from ~/.cargo/bin/walrus or PATH on creation.

    Args:
        from_cfg_path (str | None): Directory containing PytuskConfig.json.
            Defaults to ~/.pysui/.
        active_network (str | None): Override the active network ('testnet' or 'mainnet').
        persist (bool): If True, persist any overrides back to the config file.
        pysui_config_path (str | None): Override the pysui config folder path passed to
            PysuiConfiguration. Defaults to the value stored in PytuskConfig.json.
        pysui_group_name (str | None): Override the pysui ProfileGroup (protocol) for this
            session. Defaults to the active network's pysui_group_name.
        pysui_profile_name (str | None): Override the pysui Profile for this session.
            Defaults to the active network's pysui_profile_name.
        pysui_address (str | None): Set the active Sui address for this session.
        pysui_alias (str | None): Set the active Sui address by alias for this session.
    """

    _model: PytuskConfigModel
    _cfg_dir: Path

    def __init__(
        self,
        *,
        from_cfg_path: str | None = None,
        active_network: str | None = None,
        persist: bool = False,
        pysui_config_path: str | None = None,
        pysui_group_name: str | None = None,
        pysui_profile_name: str | None = None,
        pysui_address: str | None = None,
        pysui_alias: str | None = None,
    ) -> None:
        """Initialise PytuskConfiguration.

        Loads from PytuskConfig.json if it exists; auto-creates with defaults on first run.

        Args:
            from_cfg_path (str | None): Directory containing PytuskConfig.json.
                Defaults to ~/.pysui/.
            active_network (str | None): Override the active network. Must match
                a network_name present in the loaded configuration.
            persist (bool): If True, persist any overrides back to the config file.
            pysui_config_path (str | None): Override the pysui config folder path.
            pysui_group_name (str | None): Override the pysui ProfileGroup (protocol).
            pysui_profile_name (str | None): Override the pysui Profile.
            pysui_address (str | None): Set the active Sui address.
            pysui_alias (str | None): Set the active Sui address by alias.

        Raises:
            ValueError: If active_network is not a supported network.
        """
        cfg_dir = Path(from_cfg_path or _DEFAULT_CONFIG_DIR).expanduser()
        cfg_file = cfg_dir / _CONFIG_FILENAME

        if cfg_file.exists():
            self._model = PytuskConfigModel.from_json(
                cfg_file.read_text(encoding="utf-8")
            )
            if self._migrate_model():
                self._write_model(cfg_dir)
        else:
            self._model = self._default_model()
            self._write_model(cfg_dir)

        loaded_names = {n.network_name for n in self._model.networks}
        missing = _RESERVED_NETWORKS - loaded_names
        if missing:
            raise ValueError(
                f"Configuration is missing required network(s): {sorted(missing)}. "
                "The 'testnet' and 'mainnet' entries must always be present."
            )

        if active_network is not None:
            known = {n.network_name for n in self._model.networks}
            if active_network not in known:
                raise ValueError(
                    f"Unknown network '{active_network}'. "
                    f"Must be one of {sorted(known)}."
                )
            self._model.active_network = active_network

        self._cfg_dir = cfg_dir

        if persist:
            self._write_model(cfg_dir)

        self._pysui_config_path_override = pysui_config_path
        self._pysui_group_name_override = pysui_group_name
        self._pysui_profile_name_override = pysui_profile_name
        self._pysui_address = pysui_address
        self._pysui_alias = pysui_alias
        self._pysui_configuration: PysuiConfiguration | None = None

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
        model = PytuskConfigModel(
            walrus_binary_path=PytuskConfiguration._detect_walrus_binary(),
            networks=[
                WalrusNetworkConfig.from_dict(entry) for entry in _DEFAULT_NETWORKS
            ],
        )
        PytuskConfiguration._seed_default_relays(model=model)
        return model

    def _write_model(self, cfg_dir: Path) -> None:
        """Serialise the model to PytuskConfig.json in cfg_dir."""
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / _CONFIG_FILENAME).write_text(
            self._model.to_json(indent=2), encoding="utf-8"
        )

    @staticmethod
    def _seed_default_relays(*, model: PytuskConfigModel) -> None:
        """Seed built-in Mysten upload relays onto networks that ship one.

        Pre-existing relay entries are never modified or removed, and an
        already-set active_relay is never overwritten. User-defined networks
        have no built-in relay and are left with an empty list and no
        active relay.

        Args:
            model (PytuskConfigModel): Model whose networks are seeded in place.
        """
        for net in model.networks:
            defaults = _DEFAULT_RELAYS.get(net.network_name)
            if not defaults:
                continue
            known = {relay.relay_name for relay in net.relays}
            for entry in defaults:
                if entry["relay_name"] not in known:
                    net.relays.append(RelayConfig.from_dict(entry))
            if net.active_relay is None:
                net.active_relay = _MYSTEN_RELAY_NAME

    @staticmethod
    def _version_tuple(*, version: str) -> tuple[int, ...]:
        """Parse a dotted schema version into a comparable tuple.

        Args:
            version (str): Dotted version string (e.g. '1.0.0').

        Returns:
            tuple[int, ...]: Numeric components; non-numeric parts become 0.
        """
        return tuple(int(part) if part.isdigit() else 0 for part in version.split("."))

    def _migrate_model(self) -> bool:
        """Upgrade the loaded model to the current schema version.

        Migrations applied:
            1.0.0 -> 1.1.0: seed the built-in Mysten upload relays and set
            active_relay on networks that ship one. User-defined networks and
            any pre-existing relay entries are left untouched.

        Returns:
            bool: True if the model changed and must be persisted.
        """
        if self._version_tuple(version=self._model.version) >= self._version_tuple(
            version=_CONFIG_VERSION
        ):
            return False
        self._seed_default_relays(model=self._model)
        self._model.version = _CONFIG_VERSION
        return True

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
        return self._pysui_config_path_override if self._pysui_config_path_override is not None else self._model.pysui_config_path

    @property
    def pysui_group_name(self) -> str:
        """Active pysui ProfileGroup name; session override takes precedence over network default."""
        return self._pysui_group_name_override if self._pysui_group_name_override is not None else self.network.pysui_group_name

    @property
    def pysui_profile_name(self) -> str:
        """Active pysui Profile name; session override takes precedence over network default."""
        return self._pysui_profile_name_override if self._pysui_profile_name_override is not None else self.network.pysui_profile_name

    @property
    def pysui_address(self) -> str | None:
        """Active Sui address override, or None."""
        return self._pysui_address

    @property
    def pysui_alias(self) -> str | None:
        """Active Sui address alias override, or None."""
        return self._pysui_alias

    @property
    def pysui_configuration(self) -> PysuiConfiguration:
        """Fully initialised PysuiConfiguration for this session."""
        if self._pysui_configuration is None:
            self._pysui_configuration = PysuiConfiguration(
                from_cfg_path=self.pysui_config_path,
                group_name=self.pysui_group_name,
                profile_name=self.pysui_profile_name,
                address=self._pysui_address,
                alias=self._pysui_alias,
            )
        return self._pysui_configuration

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
    def network(self) -> WalrusNetworkConfig:
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

    @property
    def active_network_entry(self) -> WalrusNetworkConfig:
        """Alias for network; retained for backwards compatibility."""
        return self.network

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def set_active_network(self, network: str, *, persist: bool = False) -> None:
        """Set the active network.

        Args:
            network (str): Network name. Must match a network_name in the loaded configuration.
            persist (bool): If True, persist the change to the config file.

        Raises:
            ValueError: If network is not present in the loaded configuration.
        """
        known = {n.network_name for n in self._model.networks}
        if network not in known:
            raise ValueError(
                f"Unknown network '{network}'. "
                f"Must be one of {sorted(known)}."
            )
        self._model.active_network = network
        self._pysui_configuration = None
        if persist:
            self._write_model(self._cfg_dir)

    def add_network(self, network: WalrusNetworkConfig, *, persist: bool = False) -> None:
        """Add or replace a network configuration entry.

        If a network with the same network_name already exists it is replaced.

        Args:
            network (WalrusNetworkConfig): Network configuration to add or replace.
            persist (bool): If True, persist the change to the config file.
        """
        if network.network_name in _RESERVED_NETWORKS:
            raise ValueError(
                f"Network name '{network.network_name}' is reserved. "
                "The built-in testnet and mainnet entries cannot be replaced."
            )
        self._model.networks = [
            n for n in self._model.networks if n.network_name != network.network_name
        ]
        self._model.networks.append(network)
        self._pysui_configuration = None
        if persist:
            self._write_model(self._cfg_dir)

    def remove_network(self, network_name: str, *, persist: bool = False) -> None:
        """Remove a user-defined network configuration entry.

        Args:
            network_name (str): Name of the network to remove.
            persist (bool): If True, persist the change to the config file.

        Raises:
            ValueError: If network_name is 'testnet' or 'mainnet' (reserved).
            ValueError: If network_name is not found in the configuration.
        """
        if network_name in _RESERVED_NETWORKS:
            raise ValueError(
                f"Network '{network_name}' is reserved and cannot be removed."
            )
        if not any(n.network_name == network_name for n in self._model.networks):
            raise ValueError(
                f"Network '{network_name}' not found in configuration."
            )
        self._model.networks = [
            n for n in self._model.networks if n.network_name != network_name
        ]
        if persist:
            self._write_model(self._cfg_dir)

    def set_pysui_config_path(self, path: str, *, persist: bool = False) -> None:
        """Set the pysui config folder path.

        Args:
            path (str): Filesystem path to the pysui config folder.
            persist (bool): If True, persist the change to the config file.
        """
        self._model.pysui_config_path = str(Path(path).expanduser())
        self._pysui_configuration = None
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

    def set_walrus_aggregator_url(self, *, network_name: str, url: str, persist: bool = False) -> None:
        """Set the Walrus aggregator URL for a network.

        Unlike add_network/remove_network, this method may target reserved
        networks (testnet, mainnet) since it only updates a single field
        rather than replacing the whole network entry.

        Args:
            network_name (str): Name of the network to update.
            url (str): Walrus aggregator URL for read operations.
            persist (bool): If True, persist the change to the config file.

        Raises:
            ValueError: If network_name is not found in the configuration.
        """
        for net in self._model.networks:
            if net.network_name == network_name:
                net.walrus_aggregator_url = url
                if persist:
                    self._write_model(self._cfg_dir)
                return
        raise ValueError(f"Network '{network_name}' not found in configuration.")

    def set_walrus_publisher_url(self, *, network_name: str, url: str, persist: bool = False) -> None:
        """Set the Walrus publisher URL for a network.

        Unlike add_network/remove_network, this method may target reserved
        networks (testnet, mainnet) since it only updates a single field
        rather than replacing the whole network entry. This is the intended
        way to configure a self-hosted publisher for mainnet, which ships
        with an empty default (Mysten runs no public unauthenticated
        mainnet publisher).

        Args:
            network_name (str): Name of the network to update.
            url (str): Walrus publisher URL for write operations.
            persist (bool): If True, persist the change to the config file.

        Raises:
            ValueError: If network_name is not found in the configuration.
        """
        for net in self._model.networks:
            if net.network_name == network_name:
                net.walrus_publisher_url = url
                if persist:
                    self._write_model(self._cfg_dir)
                return
        raise ValueError(f"Network '{network_name}' not found in configuration.")

    def set_exchange_objects(
        self, *, network_name: str, exchange_objects: list[str], persist: bool = False
    ) -> None:
        """Set the WAL/SUI exchange object IDs for a network.

        Unlike add_network/remove_network, this method may target reserved
        networks (testnet, mainnet) since it only updates a single field
        rather than replacing the whole network entry. Mainnet ships with
        an empty default — use this to register mainnet exchange object
        IDs once available, or to point at custom/self-deployed exchange
        objects.

        Args:
            network_name (str): Name of the network to update.
            exchange_objects (list[str]): WAL/SUI exchange object IDs for
                this network.
            persist (bool): If True, persist the change to the config file.

        Raises:
            ValueError: If network_name is not found in the configuration.
        """
        for net in self._model.networks:
            if net.network_name == network_name:
                net.exchange_objects = exchange_objects
                if persist:
                    self._write_model(self._cfg_dir)
                return
        raise ValueError(f"Network '{network_name}' not found in configuration.")

    def _network_for(self, *, network_name: str) -> WalrusNetworkConfig:
        """Return the network entry with the given name.

        Args:
            network_name (str): Name of the network to look up.

        Returns:
            WalrusNetworkConfig: The matching network entry.

        Raises:
            ValueError: If network_name is not found in the configuration.
        """
        for net in self._model.networks:
            if net.network_name == network_name:
                return net
        raise ValueError(f"Network '{network_name}' not found in configuration.")

    def add_relay(
        self,
        *,
        network_name: str,
        relay_name: str,
        relay_url: str,
        make_active: bool = False,
        persist: bool = False,
    ) -> None:
        """Add a named upload relay to a network.

        Args:
            network_name (str): Name of the network to add the relay to.
            relay_name (str): Unique name for the relay within that network.
            relay_url (str): Base URL of the upload relay service.
            make_active (bool): If True, set the network's active_relay to relay_name.
            persist (bool): If True, persist the change to the config file.

        Raises:
            ValueError: If network_name is not found, or relay_name already
                exists on that network.
        """
        net = self._network_for(network_name=network_name)
        if any(relay.relay_name == relay_name for relay in net.relays):
            raise ValueError(
                f"Relay '{relay_name}' already exists on network '{network_name}'. "
                "Use update_relay to change its URL."
            )
        net.relays.append(RelayConfig(relay_name=relay_name, relay_url=relay_url))
        if make_active:
            net.active_relay = relay_name
        if persist:
            self._write_model(self._cfg_dir)

    def update_relay(
        self,
        *,
        network_name: str,
        relay_name: str,
        relay_url: str,
        make_active: bool = False,
        persist: bool = False,
    ) -> None:
        """Update the URL of an existing relay on a network.

        Args:
            network_name (str): Name of the network owning the relay.
            relay_name (str): Name of the relay to update.
            relay_url (str): New base URL for the upload relay service.
            make_active (bool): If True, set the network's active_relay to relay_name.
            persist (bool): If True, persist the change to the config file.

        Raises:
            ValueError: If network_name or relay_name is not found.
        """
        net = self._network_for(network_name=network_name)
        for relay in net.relays:
            if relay.relay_name == relay_name:
                relay.relay_url = relay_url
                if make_active:
                    net.active_relay = relay_name
                if persist:
                    self._write_model(self._cfg_dir)
                return
        raise ValueError(f"Relay '{relay_name}' not found on network '{network_name}'.")

    def remove_relay(
        self, *, network_name: str, relay_name: str, persist: bool = False
    ) -> None:
        """Remove a named relay from a network.

        Clears the network's active_relay when it names the relay being
        removed, so no dangling pointer is left behind.

        Args:
            network_name (str): Name of the network owning the relay.
            relay_name (str): Name of the relay to remove.
            persist (bool): If True, persist the change to the config file.

        Raises:
            ValueError: If network_name or relay_name is not found.
        """
        net = self._network_for(network_name=network_name)
        for index, relay in enumerate(net.relays):
            if relay.relay_name == relay_name:
                del net.relays[index]
                if net.active_relay == relay_name:
                    net.active_relay = None
                if persist:
                    self._write_model(self._cfg_dir)
                return
        raise ValueError(f"Relay '{relay_name}' not found on network '{network_name}'.")

    def relay_url_for(
        self, *, network_name: str, relay_name: str | None = None
    ) -> str:
        """Resolve an upload relay URL for a network.

        Resolution order is the relay_name argument, then the network's
        active_relay. If neither resolves, raises naming the relays that are
        available on that network.

        Args:
            network_name (str): Name of the network to resolve against.
            relay_name (str | None): Explicit relay name, or None to use the
                network's active_relay.

        Returns:
            str: Base URL of the resolved upload relay.

        Raises:
            ValueError: If network_name is not found, no relay resolves, or
                the resolved relay name is not present on that network.
        """
        net = self._network_for(network_name=network_name)
        available = sorted(relay.relay_name for relay in net.relays)
        resolved = relay_name or net.active_relay
        if resolved is None:
            raise ValueError(
                f"No relay named and network '{network_name}' has no active relay. "
                f"Available relays: {available}."
            )
        for relay in net.relays:
            if relay.relay_name == resolved:
                return relay.relay_url
        raise ValueError(
            f"Relay '{resolved}' not found on network '{network_name}'. "
            f"Available relays: {available}."
        )

    def relays_for(self, *, network_name: str) -> list[RelayConfig]:
        """List the upload relays configured for a network.

        Returns a new list, so adding to or removing from the result does
        not alter the configuration.

        Args:
            network_name (str): Name of the network to list relays for.

        Returns:
            list[RelayConfig]: The network's relays in configured order,
                empty when the network has none.

        Raises:
            ValueError: If network_name is not found.
        """
        return list(self._network_for(network_name=network_name).relays)

    def active_relay_for(self, *, network_name: str) -> str | None:
        """Name of a network's active upload relay, when it has one.

        Returns None when the network has no active relay. That is a
        legitimate state rather than an error: remove_relay clears
        active_relay when the relay it names is the one being removed, so
        no dangling pointer is left behind.

        Args:
            network_name (str): Name of the network to resolve against.

        Returns:
            str | None: Name of the active relay, or None when none is set.

        Raises:
            ValueError: If network_name is not found.
        """
        return self._network_for(network_name=network_name).active_relay

    def save(self) -> None:
        """Persist the current configuration to disk."""
        self._write_model(self._cfg_dir)

    def save_to(self, path: str) -> None:
        """Persist the current configuration to an alternate directory.

        Args:
            path (str): Directory path to write PytuskConfig.json into.
        """
        self._write_model(Path(path).expanduser())
