#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for Walrus committee assembly and signers bitmap packing."""

import base64
from dataclasses import FrozenInstanceError

import pytest

from pytusk.core.chain.committee import (
    JsonValue,
    WalrusCommittee,
    WalrusCommitteeMember,
    _require_dict,
    _require_int,
    _require_list,
    _require_str,
    build_committee,
    pack_signers_bitmap,
    pool_network_address,
    pool_node_id,
    pool_public_key,
    protobuf_json_to_python,
    staking_committee_shards,
    staking_epoch,
    staking_n_shards,
    staking_pools_size,
    staking_pools_table_id,
    unpack_signers_bitmap,
)

_NODE_A = "0xaa"
_NODE_B = "0xbb"
_NODE_DEAD = "0xdead"

_KEY_A = bytes(range(96))
_KEY_B = bytes(range(95, -1, -1))
_KEY_DEAD = bytes(96)


def _pool(*, node_id: str, address: str, key: bytes) -> dict[str, JsonValue]:
    """Build a decoded StakingPool payload shaped like the on-chain rendering."""
    return {
        "node_info": {
            "node_id": node_id,
            "network_address": address,
            "public_key": {"bytes": base64.b64encode(key).decode()},
            "network_public_key": "A6ozgxhbRbegMPh9GXtwnMf8dk+cBC6ALsXjCdLl2Xm8",
        },
        "wal_balance": "0",
        "latest_epoch": 489.0,
    }


def _staking_inner() -> dict[str, JsonValue]:
    """Build a decoded StakingInnerV1 payload with committee order B then A."""
    return {
        "epoch": 489.0,
        "n_shards": 4.0,
        "pools": {"id": "0x71462e68", "size": "138"},
        "committee": {
            "pos0": {
                "contents": [
                    {"key": _NODE_B, "value": [0.0, 1.0, 2.0]},
                    {"key": _NODE_A, "value": [3.0]},
                ]
            }
        },
    }


def _pools_map() -> dict[str, dict[str, JsonValue]]:
    """Build a pools lookup that is a strict superset of the committee."""
    return {
        _NODE_A: _pool(node_id=_NODE_A, address="a.example.com:9185", key=_KEY_A),
        _NODE_B: _pool(node_id=_NODE_B, address="b.example.com:9185", key=_KEY_B),
        _NODE_DEAD: _pool(node_id=_NODE_DEAD, address="0.0.0.0:9185", key=_KEY_DEAD),
    }


class TestPackSignersBitmap:
    """Packing committee positions into a signers_bitmap."""

    def test_round_trip(self) -> None:
        """Packing then unpacking returns the original ascending positions."""
        packed = pack_signers_bitmap(signer_positions=[0, 3, 8, 9], committee_size=12)
        assert unpack_signers_bitmap(bitmap=packed, committee_size=12) == (0, 3, 8, 9)

    def test_lsb_first_within_byte(self) -> None:
        """Bits are set LSB-first within each byte."""
        packed = pack_signers_bitmap(signer_positions=[0, 3, 8, 9], committee_size=12)
        assert packed == bytes([0x09, 0x03])

    def test_empty_signer_set(self) -> None:
        """No signers yields an all-zero bitmap of the correct length."""
        packed = pack_signers_bitmap(signer_positions=[], committee_size=12)
        assert packed == bytes(2)
        assert unpack_signers_bitmap(bitmap=packed, committee_size=12) == ()

    def test_length_derives_from_committee_size(self) -> None:
        """Bitmap length follows committee size, not shard count."""
        assert len(pack_signers_bitmap(signer_positions=[], committee_size=101)) == 13
        assert len(pack_signers_bitmap(signer_positions=[], committee_size=8)) == 1
        assert len(pack_signers_bitmap(signer_positions=[], committee_size=9)) == 2

    def test_position_at_committee_size_raises(self) -> None:
        """A position equal to committee_size is out of range."""
        with pytest.raises(ValueError):
            pack_signers_bitmap(signer_positions=[12], committee_size=12)

    def test_negative_position_raises(self) -> None:
        """A negative signer position is rejected."""
        with pytest.raises(ValueError):
            pack_signers_bitmap(signer_positions=[-1], committee_size=12)

    def test_negative_committee_size_raises(self) -> None:
        """A negative committee size is rejected."""
        with pytest.raises(ValueError):
            pack_signers_bitmap(signer_positions=[], committee_size=-1)


class TestUnpackSignersBitmap:
    """Unpacking a signers_bitmap back into committee positions."""

    def test_ignores_padding_bits(self) -> None:
        """Bits beyond committee_size are padding and are ignored."""
        assert unpack_signers_bitmap(bitmap=bytes([0xFF, 0xFF]), committee_size=4) == (
            0,
            1,
            2,
            3,
        )

    def test_short_bitmap_raises(self) -> None:
        """A bitmap too short for the committee is rejected."""
        with pytest.raises(ValueError):
            unpack_signers_bitmap(bitmap=bytes(1), committee_size=12)

    def test_negative_committee_size_raises(self) -> None:
        """A negative committee size is rejected."""
        with pytest.raises(ValueError):
            unpack_signers_bitmap(bitmap=bytes(2), committee_size=-1)


class TestWalrusCommittee:
    """Committee container behaviour."""

    def _committee(self) -> WalrusCommittee:
        """Build a two member committee in a fixed order."""
        return WalrusCommittee(
            epoch=489,
            n_shards=4,
            members=(
                WalrusCommitteeMember(
                    node_id=_NODE_B,
                    shard_indices=(0, 1, 2),
                    network_address="b.example.com:9185",
                    public_key=_KEY_B,
                ),
                WalrusCommitteeMember(
                    node_id=_NODE_A,
                    shard_indices=(3,),
                    network_address="a.example.com:9185",
                    public_key=_KEY_A,
                ),
            ),
        )

    def test_committee_size_is_member_count(self) -> None:
        """committee_size counts members, not shards."""
        committee = self._committee()
        assert committee.committee_size == 2
        assert committee.n_shards == 4

    def test_position_of(self) -> None:
        """position_of returns the index within the committee ordering."""
        committee = self._committee()
        assert committee.position_of(node_id=_NODE_B) == 0
        assert committee.position_of(node_id=_NODE_A) == 1

    def test_position_of_unknown_raises(self) -> None:
        """An unknown node id raises KeyError."""
        with pytest.raises(KeyError):
            self._committee().position_of(node_id=_NODE_DEAD)

    def test_member_is_frozen(self) -> None:
        """Members are immutable."""
        member = self._committee().members[0]
        with pytest.raises(FrozenInstanceError):
            member.node_id = "0xzz"  # type: ignore[misc]


class TestRequireGuards:
    """Shape guards over decoded on-chain JSON."""

    def test_require_int_narrows_integral_float(self) -> None:
        """On-chain floats such as 489.0 narrow to int."""
        assert _require_int(node=489.0, path="epoch") == 489

    def test_require_int_rejects_bool(self) -> None:
        """Booleans are not silently coerced to 0/1."""
        with pytest.raises(TypeError):
            _require_int(node=True, path="epoch")

    def test_require_int_rejects_str(self) -> None:
        """A string is not a number."""
        with pytest.raises(TypeError):
            _require_int(node="489", path="epoch")

    def test_require_int_rejects_fractional(self) -> None:
        """A fractional float is not an integer."""
        with pytest.raises(ValueError):
            _require_int(node=489.5, path="epoch")

    def test_require_dict_rejects_non_dict(self) -> None:
        """A non-mapping raises TypeError."""
        with pytest.raises(TypeError):
            _require_dict(node=[], path="pools")

    def test_require_list_rejects_non_list(self) -> None:
        """A non-list raises TypeError."""
        with pytest.raises(TypeError):
            _require_list(node={}, path="contents")

    def test_require_str_rejects_non_str(self) -> None:
        """A non-string raises TypeError."""
        with pytest.raises(TypeError):
            _require_str(node=1.0, path="key")


class TestStakingExtractors:
    """Extraction from a decoded StakingInnerV1."""

    def test_epoch(self) -> None:
        """Epoch narrows from its float rendering."""
        assert staking_epoch(staking_inner=_staking_inner()) == 489

    def test_n_shards(self) -> None:
        """Shard count narrows from its float rendering."""
        assert staking_n_shards(staking_inner=_staking_inner()) == 4

    def test_pools_table_id(self) -> None:
        """The pools table object id is read from the pools node."""
        assert staking_pools_table_id(staking_inner=_staking_inner()) == "0x71462e68"

    def test_pools_size_from_string(self) -> None:
        """ObjectTable size arrives as a JSON string."""
        assert staking_pools_size(staking_inner=_staking_inner()) == 138

    def test_pools_size_from_number(self) -> None:
        """A numeric size is also accepted."""
        inner = _staking_inner()
        inner["pools"] = {"id": "0x71462e68", "size": 138.0}
        assert staking_pools_size(staking_inner=inner) == 138

    def test_pools_size_garbage_raises(self) -> None:
        """A non-numeric size string is rejected."""
        inner = _staking_inner()
        inner["pools"] = {"id": "0x71462e68", "size": "many"}
        with pytest.raises(ValueError):
            staking_pools_size(staking_inner=inner)

    def test_committee_shards_preserves_order(self) -> None:
        """Committee ordering is taken from the chain, never re-sorted."""
        entries = staking_committee_shards(staking_inner=_staking_inner())
        assert [node_id for node_id, _ in entries] == [_NODE_B, _NODE_A]

    def test_committee_shards_values(self) -> None:
        """Shard indices narrow to ints and keep their grouping."""
        entries = staking_committee_shards(staking_inner=_staking_inner())
        assert entries == ((_NODE_B, (0, 1, 2)), (_NODE_A, (3,)))


class TestPoolExtractors:
    """Extraction from a decoded StakingPool."""

    def test_node_id(self) -> None:
        """Node id is read from node_info."""
        pool = _pool(node_id=_NODE_A, address="a.example.com:9185", key=_KEY_A)
        assert pool_node_id(pool=pool) == _NODE_A

    def test_network_address(self) -> None:
        """Network address is the bare host:port string."""
        pool = _pool(node_id=_NODE_A, address="a.example.com:9185", key=_KEY_A)
        assert pool_network_address(pool=pool) == "a.example.com:9185"

    def test_public_key_is_96_bytes(self) -> None:
        """The BLS key decodes to 96 raw bytes."""
        pool = _pool(node_id=_NODE_A, address="a.example.com:9185", key=_KEY_A)
        decoded = pool_public_key(pool=pool)
        assert decoded == _KEY_A
        assert len(decoded) == 96

    def test_public_key_ignores_network_public_key(self) -> None:
        """The 33 byte transport key is not mistaken for the BLS key."""
        pool = _pool(node_id=_NODE_A, address="a.example.com:9185", key=_KEY_A)
        node_info = _require_dict(node=pool["node_info"], path="node_info")
        transport = base64.b64decode(
            _require_str(node=node_info["network_public_key"], path="node_info.network_public_key")
        )
        assert len(transport) == 33
        assert pool_public_key(pool=pool) != transport

    def test_public_key_wrong_length_raises(self) -> None:
        """A 48 byte compressed key is rejected."""
        pool = _pool(node_id=_NODE_A, address="a.example.com:9185", key=bytes(48))
        with pytest.raises(ValueError):
            pool_public_key(pool=pool)

    def test_missing_node_info_raises(self) -> None:
        """A pool without node_info raises TypeError."""
        with pytest.raises(TypeError):
            pool_node_id(pool={})


class TestBuildCommittee:
    """Assembly of staking ordering with pools lookup data."""

    def test_preserves_staking_order(self) -> None:
        """Member order follows the staking committee, not the pools map."""
        committee = build_committee(
            staking_inner=_staking_inner(), pools_by_node_id=_pools_map()
        )
        assert [member.node_id for member in committee.members] == [_NODE_B, _NODE_A]

    def test_filters_pools_superset(self) -> None:
        """Pool entries outside the committee are dropped."""
        committee = build_committee(
            staking_inner=_staking_inner(), pools_by_node_id=_pools_map()
        )
        assert committee.committee_size == 2
        assert _NODE_DEAD not in [member.node_id for member in committee.members]

    def test_populates_member_fields(self) -> None:
        """Each member carries its shards, address and BLS key."""
        committee = build_committee(
            staking_inner=_staking_inner(), pools_by_node_id=_pools_map()
        )
        first = committee.members[0]
        assert first.node_id == _NODE_B
        assert first.shard_indices == (0, 1, 2)
        assert first.network_address == "b.example.com:9185"
        assert first.public_key == _KEY_B

    def test_epoch_and_n_shards(self) -> None:
        """Epoch and shard count come from the staking state."""
        committee = build_committee(
            staking_inner=_staking_inner(), pools_by_node_id=_pools_map()
        )
        assert committee.epoch == 489
        assert committee.n_shards == 4

    def test_missing_pool_entry_raises(self) -> None:
        """A committee member absent from pools is an error, not a skip."""
        pools = _pools_map()
        del pools[_NODE_A]
        with pytest.raises(KeyError):
            build_committee(staking_inner=_staking_inner(), pools_by_node_id=pools)

    def test_positions_drive_bitmap(self) -> None:
        """Committee positions are what signers_bitmap encodes."""
        committee = build_committee(
            staking_inner=_staking_inner(), pools_by_node_id=_pools_map()
        )
        position = committee.position_of(node_id=_NODE_A)
        packed = pack_signers_bitmap(
            signer_positions=[position], committee_size=committee.committee_size
        )
        assert unpack_signers_bitmap(
            bitmap=packed, committee_size=committee.committee_size
        ) == (position,)


class _FakeValue:
    """Stand-in for a protobuf ``Value`` where only the set field exists.

    This mirrors betterproto's behaviour, which is what the converter relies
    on: an unset oneof member reads back as ``None`` (here, absent entirely)
    rather than as the field default.
    """

    def __init__(self, **fields: object) -> None:
        """Set only the attributes given, leaving the rest absent."""
        for name, value in fields.items():
            setattr(self, name, value)


class _FakeStruct:
    """Stand-in for a protobuf ``Struct``."""

    def __init__(self, *, fields: dict[str, object]) -> None:
        """Hold the named fields."""
        self.fields = fields


class _FakeList:
    """Stand-in for a protobuf ``ListValue``."""

    def __init__(self, *, values: list[object]) -> None:
        """Hold the ordered values."""
        self.values = values


class TestProtobufJsonToPython:
    """Conversion of the protobuf Value rendering into plain Python."""

    def test_string(self) -> None:
        """A string value converts to str."""
        assert protobuf_json_to_python(value=_FakeValue(string_value="x")) == "x"

    def test_zero_number_is_not_confused_with_unset(self) -> None:
        """A numeric zero survives; it is falsy but set."""
        assert protobuf_json_to_python(value=_FakeValue(number_value=0.0)) == 0.0

    def test_false_bool_is_not_confused_with_unset(self) -> None:
        """A False boolean survives; it is falsy but set."""
        assert protobuf_json_to_python(value=_FakeValue(bool_value=False)) is False

    def test_empty_string_survives(self) -> None:
        """An empty string is a set value, not an absent one."""
        assert protobuf_json_to_python(value=_FakeValue(string_value="")) == ""

    def test_nested_struct(self) -> None:
        """A struct converts to a dict, recursively."""
        inner = _FakeValue(
            struct_value=_FakeStruct(fields={"b": _FakeValue(number_value=2.0)})
        )
        outer = _FakeValue(struct_value=_FakeStruct(fields={"a": inner}))
        assert protobuf_json_to_python(value=outer) == {"a": {"b": 2.0}}

    def test_list_of_structs(self) -> None:
        """A list converts to a list, recursively."""
        entry = _FakeValue(
            struct_value=_FakeStruct(fields={"k": _FakeValue(string_value="v")})
        )
        value = _FakeValue(list_value=_FakeList(values=[entry]))
        assert protobuf_json_to_python(value=value) == [{"k": "v"}]

    def test_empty_struct(self) -> None:
        """An empty struct converts to an empty dict."""
        value = _FakeValue(struct_value=_FakeStruct(fields={}))
        assert protobuf_json_to_python(value=value) == {}

    def test_all_unset_is_none(self) -> None:
        """A value with nothing set converts to None."""
        assert protobuf_json_to_python(value=_FakeValue()) is None

    def test_none_input(self) -> None:
        """A None input converts to None."""
        assert protobuf_json_to_python(value=None) is None


class TestShardLookup:
    """Shard index to member routing."""

    def _committee(self) -> WalrusCommittee:
        """Build a committee covering shards 0-3 across two members."""
        return build_committee(
            staking_inner=_staking_inner(), pools_by_node_id=_pools_map()
        )

    def test_position_for_shard(self) -> None:
        """Each shard maps to the position of its holding member."""
        committee = self._committee()
        assert committee.position_for_shard(shard_index=0) == 0
        assert committee.position_for_shard(shard_index=2) == 0
        assert committee.position_for_shard(shard_index=3) == 1

    def test_member_for_shard(self) -> None:
        """Each shard maps to its holding member."""
        committee = self._committee()
        assert committee.member_for_shard(shard_index=1).node_id == _NODE_B
        assert committee.member_for_shard(shard_index=3).node_id == _NODE_A

    def test_shard_out_of_range_raises(self) -> None:
        """A shard index beyond n_shards is rejected."""
        committee = self._committee()
        with pytest.raises(ValueError):
            committee.position_for_shard(shard_index=4)

    def test_negative_shard_raises(self) -> None:
        """A negative shard index is rejected."""
        committee = self._committee()
        with pytest.raises(ValueError):
            committee.member_for_shard(shard_index=-1)

    def test_lookup_agrees_with_position_of(self) -> None:
        """Shard routing and node lookup agree on position."""
        committee = self._committee()
        for shard in range(committee.n_shards):
            member = committee.member_for_shard(shard_index=shard)
            assert committee.position_of(node_id=member.node_id) == (
                committee.position_for_shard(shard_index=shard)
            )


class TestShardCoverageValidation:
    """build_committee rejects incoherent shard assignments."""

    def test_uncovered_shard_raises(self) -> None:
        """A shard held by nobody is an error."""
        inner = _staking_inner()
        inner["n_shards"] = 8.0
        with pytest.raises(ValueError):
            build_committee(staking_inner=inner, pools_by_node_id=_pools_map())

    def test_duplicated_shard_raises(self) -> None:
        """A shard claimed by two members is an error."""
        inner = _staking_inner()
        committee = _require_dict(node=inner["committee"], path="committee")
        vec_map = _require_dict(node=committee["pos0"], path="committee.pos0")
        contents = _require_list(node=vec_map["contents"], path="committee.contents")
        entry = _require_dict(node=contents[1], path="committee.contents[1]")
        duplicate_shard: list[JsonValue] = [0.0]
        entry["value"] = duplicate_shard
        with pytest.raises(ValueError):
            build_committee(staking_inner=inner, pools_by_node_id=_pools_map())

    def test_shard_beyond_n_shards_raises(self) -> None:
        """A member claiming a shard outside range is an error."""
        inner = _staking_inner()
        committee = _require_dict(node=inner["committee"], path="committee")
        vec_map = _require_dict(node=committee["pos0"], path="committee.pos0")
        contents = _require_list(node=vec_map["contents"], path="committee.contents")
        entry = _require_dict(node=contents[1], path="committee.contents[1]")
        out_of_range_shard: list[JsonValue] = [99.0]
        entry["value"] = out_of_range_shard
        with pytest.raises(ValueError):
            build_committee(staking_inner=inner, pools_by_node_id=_pools_map())


class TestMemberBaseUrl:
    """Base URL composition for a committee member."""

    def test_base_url_adds_scheme(self) -> None:
        """The bare host:port gains an https scheme."""
        member = WalrusCommitteeMember(
            node_id=_NODE_A,
            shard_indices=(0,),
            network_address="a.example.com:9185",
            public_key=_KEY_A,
        )
        assert member.base_url == "https://a.example.com:9185"
