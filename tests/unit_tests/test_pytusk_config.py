#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for PytuskConfiguration."""

import json
from pathlib import Path

import pytest

from pytusk.config.tusk_config import (
    NetworkType,
    PytuskConfigModel,
    PytuskConfiguration,
    RelayConfig,
    WalrusNetworkConfig,
)


class TestWalrusNetworkConfig:
    def test_default_fields_raises_on_empty_name(self) -> None:
        with pytest.raises(ValueError, match="network_name must not be empty"):
            WalrusNetworkConfig()

    def test_default_fields_with_name(self) -> None:
        net = WalrusNetworkConfig(network_name="custom")
        assert net.pysui_group_name == ""
        assert net.walrus_aggregator_url == ""
        assert net.walrus_publisher_url == ""
        assert net.exchange_objects == []
        assert net.network_type == NetworkType.TEST

    def test_from_dict(self) -> None:
        net = WalrusNetworkConfig.from_dict(
            {
                "network_name": "testnet",
                "pysui_group_name": "sui_gql_config",
                "pysui_profile_name": "testnet",
                "walrus_aggregator_url": "https://aggregator.example.com",
                "walrus_publisher_url": "https://publisher.example.com",
                "system_object": "0xabc",
                "staking_object": "0xdef",
                "exchange_objects": ["0x111", "0x222"],
            }
        )
        assert net.network_name == "testnet"
        assert net.pysui_group_name == "sui_gql_config"
        assert net.walrus_aggregator_url == "https://aggregator.example.com"
        assert net.walrus_publisher_url == "https://publisher.example.com"
        assert net.exchange_objects == ["0x111", "0x222"]

    def test_to_dict_round_trip(self) -> None:
        net = WalrusNetworkConfig(
            network_name="mainnet",
            walrus_aggregator_url="https://aggregator.mainnet.example.com",
            walrus_publisher_url="",
        )
        assert WalrusNetworkConfig.from_dict(net.to_dict()) == net

    def test_network_type_default_is_test(self) -> None:
        net = WalrusNetworkConfig(network_name="custom")
        assert net.network_type == NetworkType.TEST

    def test_network_type_production(self) -> None:
        net = WalrusNetworkConfig(network_name="mainnet", network_type=NetworkType.PRODUCTION)
        assert net.network_type == NetworkType.PRODUCTION

    def test_network_type_round_trip(self) -> None:
        net = WalrusNetworkConfig(network_name="mainnet", network_type=NetworkType.PRODUCTION)
        restored = WalrusNetworkConfig.from_dict(net.to_dict())
        assert restored.network_type == NetworkType.PRODUCTION


class TestPytuskConfigModel:
    def test_defaults(self) -> None:
        model = PytuskConfigModel()
        assert model.version == "1.1.0"
        assert model.active_network == "testnet"
        assert model.networks == []
        assert model.walrus_binary_path is None

    def test_json_round_trip(self) -> None:
        model = PytuskConfigModel(active_network="mainnet")
        loaded = PytuskConfigModel.from_json(model.to_json())
        assert loaded.active_network == "mainnet"
        assert loaded.version == "1.1.0"

    def test_networks_serialise(self) -> None:
        net = WalrusNetworkConfig(
            network_name="testnet",
            walrus_aggregator_url="https://aggregator.example.com",
            walrus_publisher_url="https://publisher.example.com",
        )
        model = PytuskConfigModel(networks=[net])
        loaded = PytuskConfigModel.from_json(model.to_json())
        assert len(loaded.networks) == 1
        assert loaded.networks[0].network_name == "testnet"
        assert loaded.networks[0].walrus_aggregator_url == "https://aggregator.example.com"
        assert loaded.networks[0].walrus_publisher_url == "https://publisher.example.com"


class TestPytuskConfiguration:
    def test_first_run_creates_file(self, tmp_path: Path) -> None:
        PytuskConfiguration(from_cfg_path=str(tmp_path))
        assert (tmp_path / "PytuskConfig.json").exists()

    def test_first_run_default_network(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        assert cfg.active_network == "testnet"

    def test_first_run_two_networks(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        names = [n.network_name for n in cfg.networks]
        assert "testnet" in names
        assert "mainnet" in names

    def test_loads_existing_file(self, tmp_path: Path) -> None:
        PytuskConfiguration(from_cfg_path=str(tmp_path))
        cfg2 = PytuskConfiguration(from_cfg_path=str(tmp_path))
        assert cfg2.active_network == "testnet"
        assert len(cfg2.networks) == 2

    def test_active_network_override(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path), active_network="mainnet")
        assert cfg.active_network == "mainnet"

    def test_invalid_network_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Unknown network"):
            PytuskConfiguration(from_cfg_path=str(tmp_path), active_network="devnet")

    def test_active_network_entry_testnet(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        entry = cfg.active_network_entry
        assert entry.network_name == "testnet"
        assert entry.walrus_aggregator_url != ""
        assert entry.walrus_publisher_url != ""
        assert entry.network_type == NetworkType.TEST

    def test_active_network_entry_mainnet(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path), active_network="mainnet")
        entry = cfg.active_network_entry
        assert entry.network_name == "mainnet"
        assert entry.walrus_aggregator_url != ""
        assert entry.walrus_publisher_url == ""
        assert entry.network_type == NetworkType.PRODUCTION

    def test_set_active_network(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        cfg.set_active_network("mainnet")
        assert cfg.active_network == "mainnet"

    def test_set_active_network_invalid_raises(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        with pytest.raises(ValueError, match="Unknown network"):
            cfg.set_active_network("devnet")

    def test_set_active_network_persist(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        cfg.set_active_network("mainnet", persist=True)
        cfg2 = PytuskConfiguration(from_cfg_path=str(tmp_path))
        assert cfg2.active_network == "mainnet"

    def test_set_pysui_config_path(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        cfg.set_pysui_config_path("/custom/pysui")
        assert cfg.pysui_config_path == "/custom/pysui"

    def test_set_pysui_config_path_persist(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        cfg.set_pysui_config_path("/custom/pysui", persist=True)
        cfg2 = PytuskConfiguration(from_cfg_path=str(tmp_path))
        assert cfg2.pysui_config_path == "/custom/pysui"

    def test_set_walrus_binary_path(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        cfg.set_walrus_binary_path("/usr/local/bin/walrus")
        assert cfg.walrus_binary_path == "/usr/local/bin/walrus"

    def test_set_walrus_binary_path_persist(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        cfg.set_walrus_binary_path("/usr/local/bin/walrus", persist=True)
        cfg2 = PytuskConfiguration(from_cfg_path=str(tmp_path))
        assert cfg2.walrus_binary_path == "/usr/local/bin/walrus"

    def test_set_walrus_aggregator_url(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        cfg.set_walrus_aggregator_url(network_name="testnet", url="https://custom-aggregator.example.com")
        assert cfg.network.walrus_aggregator_url == "https://custom-aggregator.example.com"

    def test_set_walrus_aggregator_url_persist(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        cfg.set_walrus_aggregator_url(network_name="testnet", url="https://custom-aggregator.example.com", persist=True)
        cfg2 = PytuskConfiguration(from_cfg_path=str(tmp_path))
        assert cfg2.network.walrus_aggregator_url == "https://custom-aggregator.example.com"

    def test_set_walrus_aggregator_url_not_found_raises(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        with pytest.raises(ValueError, match="not found"):
            cfg.set_walrus_aggregator_url(network_name="nonexistent", url="https://custom-aggregator.example.com")

    def test_set_walrus_publisher_url(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path), active_network="mainnet")
        cfg.set_walrus_publisher_url(network_name="mainnet", url="https://my-publisher.example.com")
        assert cfg.network.walrus_publisher_url == "https://my-publisher.example.com"

    def test_set_walrus_publisher_url_persist(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path), active_network="mainnet")
        cfg.set_walrus_publisher_url(network_name="mainnet", url="https://my-publisher.example.com", persist=True)
        cfg2 = PytuskConfiguration(from_cfg_path=str(tmp_path), active_network="mainnet")
        assert cfg2.network.walrus_publisher_url == "https://my-publisher.example.com"

    def test_set_walrus_publisher_url_not_found_raises(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        with pytest.raises(ValueError, match="not found"):
            cfg.set_walrus_publisher_url(network_name="nonexistent", url="https://my-publisher.example.com")

    def test_set_exchange_objects(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        cfg.set_exchange_objects(network_name="testnet", exchange_objects=["0xabc", "0xdef"])
        assert cfg.network.exchange_objects == ["0xabc", "0xdef"]

    def test_set_exchange_objects_persist(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        cfg.set_exchange_objects(network_name="testnet", exchange_objects=["0xabc", "0xdef"], persist=True)
        cfg2 = PytuskConfiguration(from_cfg_path=str(tmp_path))
        assert cfg2.network.exchange_objects == ["0xabc", "0xdef"]

    def test_set_exchange_objects_not_found_raises(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        with pytest.raises(ValueError, match="not found"):
            cfg.set_exchange_objects(network_name="nonexistent", exchange_objects=["0xabc"])

    def test_save(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        cfg.set_active_network("mainnet")
        cfg.save()
        cfg2 = PytuskConfiguration(from_cfg_path=str(tmp_path))
        assert cfg2.active_network == "mainnet"

    def test_save_to(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        alt_dir = tmp_path / "alt"
        cfg.save_to(str(alt_dir))
        assert (alt_dir / "PytuskConfig.json").exists()

    def test_json_structure(self, tmp_path: Path) -> None:
        PytuskConfiguration(from_cfg_path=str(tmp_path))
        data = json.loads((tmp_path / "PytuskConfig.json").read_text())
        assert data["version"] == "1.1.0"
        assert data["active_network"] == "testnet"
        assert isinstance(data["networks"], list)
        assert len(data["networks"]) == 2

    def test_persist_on_init(self, tmp_path: Path) -> None:
        PytuskConfiguration(from_cfg_path=str(tmp_path), active_network="mainnet", persist=True)
        cfg2 = PytuskConfiguration(from_cfg_path=str(tmp_path))
        assert cfg2.active_network == "mainnet"

    def test_add_network_new(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        custom = WalrusNetworkConfig(
            network_name="custom",
            walrus_aggregator_url="https://daemon.example.com",
            system_object="0xabc",
            staking_object="0xdef",
        )
        cfg.add_network(custom)
        names = [n.network_name for n in cfg.networks]
        assert "custom" in names

    def test_add_network_replaces_existing(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        custom = WalrusNetworkConfig(
            network_name="custom",
            walrus_aggregator_url="https://daemon.example.com",
            system_object="0xabc",
            staking_object="0xdef",
        )
        cfg.add_network(custom)
        updated = WalrusNetworkConfig(
            network_name="custom",
            walrus_aggregator_url="https://new-daemon.example.com",
            system_object="0x111",
            staking_object="0x222",
        )
        cfg.add_network(updated)
        custom_entries = [n for n in cfg.networks if n.network_name == "custom"]
        assert len(custom_entries) == 1
        assert custom_entries[0].walrus_aggregator_url == "https://new-daemon.example.com"

    def test_add_network_persist(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        custom = WalrusNetworkConfig(
            network_name="custom",
            walrus_aggregator_url="https://daemon.example.com",
            system_object="0xabc",
            staking_object="0xdef",
        )
        cfg.add_network(custom, persist=True)
        cfg2 = PytuskConfiguration(from_cfg_path=str(tmp_path))
        names = [n.network_name for n in cfg2.networks]
        assert "custom" in names

    def test_add_network_then_activate(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        custom = WalrusNetworkConfig(
            network_name="custom",
            walrus_aggregator_url="https://daemon.example.com",
            system_object="0xabc",
            staking_object="0xdef",
        )
        cfg.add_network(custom)
        cfg.set_active_network("custom")
        assert cfg.active_network == "custom"
        assert cfg.active_network_entry.walrus_aggregator_url == "https://daemon.example.com"

    def test_add_network_reserved_testnet_raises(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        with pytest.raises(ValueError, match="reserved"):
            cfg.add_network(WalrusNetworkConfig(network_name="testnet"))

    def test_add_network_reserved_mainnet_raises(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        with pytest.raises(ValueError, match="reserved"):
            cfg.add_network(WalrusNetworkConfig(network_name="mainnet"))

    def test_remove_network(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        custom = WalrusNetworkConfig(
            network_name="custom",
            walrus_aggregator_url="https://daemon.example.com",
            system_object="0xabc",
            staking_object="0xdef",
        )
        cfg.add_network(custom)
        cfg.remove_network("custom")
        assert not any(n.network_name == "custom" for n in cfg.networks)

    def test_remove_network_persist(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        custom = WalrusNetworkConfig(
            network_name="custom",
            walrus_aggregator_url="https://daemon.example.com",
            system_object="0xabc",
            staking_object="0xdef",
        )
        cfg.add_network(custom, persist=True)
        cfg.remove_network("custom", persist=True)
        cfg2 = PytuskConfiguration(from_cfg_path=str(tmp_path))
        assert not any(n.network_name == "custom" for n in cfg2.networks)

    def test_remove_network_reserved_testnet_raises(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        with pytest.raises(ValueError, match="reserved"):
            cfg.remove_network("testnet")

    def test_remove_network_reserved_mainnet_raises(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        with pytest.raises(ValueError, match="reserved"):
            cfg.remove_network("mainnet")

    def test_remove_network_not_found_raises(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        with pytest.raises(ValueError, match="not found"):
            cfg.remove_network("nonexistent")

    def test_init_fails_if_testnet_missing(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        cfg._model.networks = [n for n in cfg._model.networks if n.network_name != "testnet"]
        cfg.save()
        with pytest.raises(ValueError, match="missing required"):
            PytuskConfiguration(from_cfg_path=str(tmp_path))

    def test_init_fails_if_mainnet_missing(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        cfg._model.networks = [n for n in cfg._model.networks if n.network_name != "mainnet"]
        cfg.save()
        with pytest.raises(ValueError, match="missing required"):
            PytuskConfiguration(from_cfg_path=str(tmp_path))


class TestRelayConfig:
    """RelayConfig dataclass behaviour."""

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="relay_name must not be empty"):
            RelayConfig()

    def test_json_round_trip(self) -> None:
        relay = RelayConfig(relay_name="mysten", relay_url="https://relay.example.com")
        loaded = RelayConfig.from_dict(relay.to_dict())
        assert loaded.relay_name == "mysten"
        assert loaded.relay_url == "https://relay.example.com"


class TestRelayMigration:
    """Config schema migration from 1.0.0 to 1.1.0."""

    @staticmethod
    def _write_v1_config(tmp_path: Path, networks: list[WalrusNetworkConfig]) -> None:
        """Write a 1.0.0-shaped PytuskConfig.json with no relay fields."""
        model = PytuskConfigModel(version="1.0.0", networks=networks)
        data = json.loads(model.to_json())
        for entry in data["networks"]:
            entry.pop("relays", None)
            entry.pop("active_relay", None)
        (tmp_path / "PytuskConfig.json").write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )

    def test_seeds_builtin_relays(self, tmp_path: Path) -> None:
        self._write_v1_config(
            tmp_path,
            [
                WalrusNetworkConfig(network_name="testnet"),
                WalrusNetworkConfig(network_name="mainnet"),
            ],
        )
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        by_name = {n.network_name: n for n in cfg.networks}
        assert [r.relay_name for r in by_name["testnet"].relays] == ["mysten"]
        assert (
            by_name["testnet"].relays[0].relay_url
            == "https://upload-relay.testnet.walrus.space"
        )
        assert by_name["testnet"].active_relay == "mysten"
        assert (
            by_name["mainnet"].relays[0].relay_url
            == "https://upload-relay.mainnet.walrus.space"
        )
        assert by_name["mainnet"].active_relay == "mysten"

    def test_bumps_version_and_persists_to_source_dir(self, tmp_path: Path) -> None:
        self._write_v1_config(
            tmp_path,
            [
                WalrusNetworkConfig(network_name="testnet"),
                WalrusNetworkConfig(network_name="mainnet"),
            ],
        )
        PytuskConfiguration(from_cfg_path=str(tmp_path))
        data = json.loads((tmp_path / "PytuskConfig.json").read_text())
        assert data["version"] == "1.1.0"
        by_name = {n["network_name"]: n for n in data["networks"]}
        assert by_name["testnet"]["active_relay"] == "mysten"

    def test_user_defined_network_gets_no_relay(self, tmp_path: Path) -> None:
        self._write_v1_config(
            tmp_path,
            [
                WalrusNetworkConfig(network_name="testnet"),
                WalrusNetworkConfig(network_name="mainnet"),
                WalrusNetworkConfig(network_name="custom"),
            ],
        )
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        by_name = {n.network_name: n for n in cfg.networks}
        assert by_name["custom"].relays == []
        assert by_name["custom"].active_relay is None

    def test_existing_relays_not_clobbered(self, tmp_path: Path) -> None:
        testnet = WalrusNetworkConfig(network_name="testnet")
        testnet.relays = [
            RelayConfig(relay_name="mine", relay_url="https://mine.example.com")
        ]
        testnet.active_relay = "mine"
        model = PytuskConfigModel(
            version="1.0.0",
            networks=[testnet, WalrusNetworkConfig(network_name="mainnet")],
        )
        (tmp_path / "PytuskConfig.json").write_text(
            model.to_json(indent=2), encoding="utf-8"
        )
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        by_name = {n.network_name: n for n in cfg.networks}
        assert by_name["testnet"].active_relay == "mine"
        assert [r.relay_name for r in by_name["testnet"].relays] == ["mine", "mysten"]

    def test_migration_is_idempotent(self, tmp_path: Path) -> None:
        self._write_v1_config(
            tmp_path,
            [
                WalrusNetworkConfig(network_name="testnet"),
                WalrusNetworkConfig(network_name="mainnet"),
            ],
        )
        PytuskConfiguration(from_cfg_path=str(tmp_path))
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        by_name = {n.network_name: n for n in cfg.networks}
        assert [r.relay_name for r in by_name["testnet"].relays] == ["mysten"]
        data = json.loads((tmp_path / "PytuskConfig.json").read_text())
        assert data["version"] == "1.1.0"


class TestRelayCrud:
    """add_relay / update_relay / remove_relay / relay_url_for."""

    def test_add_relay(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        cfg.add_relay(
            network_name="testnet",
            relay_name="mine",
            relay_url="https://mine.example.com",
        )
        assert (
            cfg.relay_url_for(network_name="testnet", relay_name="mine")
            == "https://mine.example.com"
        )

    def test_add_relay_make_active(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        cfg.add_relay(
            network_name="testnet",
            relay_name="mine",
            relay_url="https://mine.example.com",
            make_active=True,
        )
        assert cfg.relay_url_for(network_name="testnet") == "https://mine.example.com"

    def test_add_relay_duplicate_rejected(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        with pytest.raises(ValueError, match="already exists"):
            cfg.add_relay(
                network_name="testnet",
                relay_name="mysten",
                relay_url="https://other.example.com",
            )

    def test_add_relay_unknown_network(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        with pytest.raises(ValueError, match="not found in configuration"):
            cfg.add_relay(
                network_name="nope",
                relay_name="mine",
                relay_url="https://mine.example.com",
            )

    def test_update_relay(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        cfg.update_relay(
            network_name="testnet",
            relay_name="mysten",
            relay_url="https://moved.example.com",
        )
        assert cfg.relay_url_for(network_name="testnet") == "https://moved.example.com"

    def test_update_relay_unknown(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        with pytest.raises(ValueError, match="not found on network"):
            cfg.update_relay(
                network_name="testnet",
                relay_name="nope",
                relay_url="https://nope.example.com",
            )

    def test_remove_relay_clears_dangling_active(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        cfg.remove_relay(network_name="testnet", relay_name="mysten")
        by_name = {n.network_name: n for n in cfg.networks}
        assert by_name["testnet"].relays == []
        assert by_name["testnet"].active_relay is None

    def test_remove_relay_keeps_unrelated_active(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        cfg.add_relay(
            network_name="testnet",
            relay_name="mine",
            relay_url="https://mine.example.com",
        )
        cfg.remove_relay(network_name="testnet", relay_name="mine")
        by_name = {n.network_name: n for n in cfg.networks}
        assert by_name["testnet"].active_relay == "mysten"

    def test_remove_relay_unknown(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        with pytest.raises(ValueError, match="not found on network"):
            cfg.remove_relay(network_name="testnet", relay_name="nope")

    def test_relay_url_for_argument_wins_over_active(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        cfg.add_relay(
            network_name="testnet",
            relay_name="mine",
            relay_url="https://mine.example.com",
        )
        assert (
            cfg.relay_url_for(network_name="testnet", relay_name="mine")
            == "https://mine.example.com"
        )
        assert (
            cfg.relay_url_for(network_name="testnet")
            == "https://upload-relay.testnet.walrus.space"
        )

    def test_relay_url_for_no_active_relay(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        cfg.remove_relay(network_name="testnet", relay_name="mysten")
        with pytest.raises(ValueError, match="has no active relay"):
            cfg.relay_url_for(network_name="testnet")

    def test_relay_url_for_unknown_relay_lists_available(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        with pytest.raises(ValueError, match="Available relays"):
            cfg.relay_url_for(network_name="testnet", relay_name="nope")

    def test_relay_persist_writes_file(self, tmp_path: Path) -> None:
        cfg = PytuskConfiguration(from_cfg_path=str(tmp_path))
        cfg.add_relay(
            network_name="testnet",
            relay_name="mine",
            relay_url="https://mine.example.com",
            persist=True,
        )
        data = json.loads((tmp_path / "PytuskConfig.json").read_text())
        by_name = {n["network_name"]: n for n in data["networks"]}
        assert [r["relay_name"] for r in by_name["testnet"]["relays"]] == [
            "mysten",
            "mine",
        ]
