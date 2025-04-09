#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Walrus Configuration Datatype."""

from pathlib import Path
import dataclasses
import dataclasses_json
import yaml


@dataclasses_json.dataclass_json(letter_case=dataclasses_json.LetterCase.CAMEL)
@dataclasses.dataclass
class WalrusConfig:
    """Simple use."""

    system_object: str
    staking_object: str
    subsidies_object: str
    exchange_objects: list[str]
    wallet_config: str


# pylint: disable=no-member
def load_from_yaml(yaml_file: Path, pysui_cfg) -> WalrusConfig:
    """."""
    if not yaml_file.exists():
        raise ValueError(f"{yaml_file} does not exist")

    wcfg = yaml.safe_load(yaml_file.read_text(encoding="utf8"))
    # TODO: Change to letting the  default_context drive
    if pysui_cfg.active_profile != "testnet":
        raise ValueError("Configuration not set to testnet")
    tnet = wcfg.get("contexts").get("testnet")
    return WalrusConfig.from_dict(tnet)
