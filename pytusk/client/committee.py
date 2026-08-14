#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Walrus committee resolution and signer bitmap packing.

All knowledge of the on-chain ``StakingInnerV1`` layout is confined to this
module. That layout is the most likely thing to break across a Walrus upgrade,
so it has exactly one place to be fixed.

The module is committee-scoped: the current Walrus epoch, the ordered committee
member list, each member's shard assignment and network address, and the bitmap
that indexes positions within that ordering. The staking object is merely where
the data lives; the committee is what it is.

Committee results are never cached. A cached committee surviving an epoch
boundary would route slivers to nodes that no longer hold those shards, and the
failure would surface later at certification, far from its cause. Resolve once
per operation and pass the result down through fan-out and retries. Note that a
fresh fetch narrows the window but does not close it -- an operation can still
straddle an epoch boundary -- so certification must treat an on-chain epoch
mismatch as refetch-and-retry rather than as a signature failure. If caching is
ever added it belongs in an explicit owner object invalidated on epoch change,
never hidden inside these functions.

Chain access is expressed as the :class:`ChainReader` protocol rather than a
concrete client, so this module has no dependency on ``WalrusClient``. The
dependency points one way: clients may know about committees, committees never
know about clients.
"""

from __future__ import annotations

import base64
import dataclasses
import functools
from collections.abc import Iterable
from typing import Protocol, TypeAlias

from pysui import GetDynamicFields, SuiCommand, SuiRpcResult

__all__ = [
    "ChainReader",
    "WalrusCommittee",
    "WalrusCommitteeMember",
    "fetch_committee",
    "fetch_epoch",
    "pack_signers_bitmap",
    "unpack_signers_bitmap",
]

JsonValue: TypeAlias = (
    "str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]"
)

_UNCOMPRESSED_G1_LENGTH: int = 96
"""Byte length of an on-chain ``Element<UncompressedG1>`` BLS public key."""

_STAKING_INNER_TYPE_SUFFIX: str = "staking_inner::StakingInnerV1"
"""Move type suffix of the staking inner state this module can read."""


class ChainReader(Protocol):
    """The minimum Sui transport surface a committee fetch requires.

    ``WalrusClient`` satisfies this structurally -- no registration and no
    inheritance are needed. Depending on this protocol rather than on the
    concrete client is what keeps the fetch functions unit-testable against a
    fake, and what keeps this module free of a circular import.
    """

    async def execute(
        self,
        *,
        command: SuiCommand,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> SuiRpcResult:
        """Execute a single Sui command.

        Args:
            command (SuiCommand): The command to execute.
            timeout (float | None): Request timeout in seconds.
            headers (dict[str, str] | None): Optional transport headers.

        Returns:
            SuiRpcResult: The result of the command.
        """
        ...

    async def execute_for_all(
        self,
        *,
        command: SuiCommand,
        timeout: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> SuiRpcResult:
        """Execute a paged Sui command, collecting every page.

        Args:
            command (SuiCommand): The paged command to execute.
            timeout (float | None): Request timeout in seconds.
            headers (dict[str, str] | None): Optional transport headers.

        Returns:
            SuiRpcResult: The result carrying all pages.
        """
        ...


@dataclasses.dataclass(kw_only=True, frozen=True)
class WalrusCommitteeMember:
    """A single storage node in the Walrus committee for one epoch.

    Attributes:
        node_id (str): On-chain object ID of the node's staking pool.
        shard_indices (tuple[int, ...]): Shard indices assigned to this node.
        network_address (str): Bare ``host:port`` of the node's public API.
        public_key (bytes): BLS12-381 min_pk public key bytes for this node.
    """

    node_id: str
    shard_indices: tuple[int, ...]
    network_address: str
    public_key: bytes

    @property
    def base_url(self) -> str:
        """Return the base URL for this node's storage API.

        ``network_address`` is a bare ``host:port`` with no scheme. Composing
        the URL here gives that rule one home, rather than repeating it at
        every sliver PUT site.

        Returns:
            str: Base URL including scheme.
        """
        return f"https://{self.network_address}"


@dataclasses.dataclass(kw_only=True, frozen=True)
class WalrusCommittee:
    """The Walrus storage committee for a single epoch.

    ``members`` is an ORDERED SEQUENCE and must preserve the on-chain ordering
    of ``StakingInnerV1.committee``. The ``signers_bitmap`` index is a position
    within this ordering, and on-chain ``verify_quorum_in_epoch`` reconstructs
    the aggregate public key by walking committee members positionally.
    Reordering these members, or replacing this sequence with a mapping keyed by
    node ID, silently breaks ``certify_blob`` verification and reports the
    failure against the signature rather than the ordering. The lookup indexes
    below are built ALONGSIDE the sequence, never instead of it.

    Do NOT add ``slots=True`` to this dataclass. The indexes are
    ``functools.cached_property``, which caches by writing into the instance
    ``__dict__``; ``slots=True`` removes ``__dict__`` and the caching breaks.

    Attributes:
        epoch (int): Walrus epoch this committee is in force for.
        n_shards (int): Total shard count for the epoch. Distinct from
            ``len(members)`` -- each node holds one or more shards.
        members (tuple[WalrusCommitteeMember, ...]): Committee members in
            on-chain order.
    """

    epoch: int
    n_shards: int
    members: tuple[WalrusCommitteeMember, ...]

    @property
    def committee_size(self) -> int:
        """Return the number of committee members.

        This is the correct size for ``signers_bitmap`` allocation. Using
        ``n_shards`` produces a bitmap of the wrong length.

        Returns:
            int: Count of committee members.
        """
        return len(self.members)

    @functools.cached_property
    def _position_by_node_id(self) -> dict[str, int]:
        """Return a node ID to committee position index.

        Returns:
            dict[str, int]: Position keyed by node identifier.
        """
        return {
            member.node_id: position for position, member in enumerate(self.members)
        }

    @functools.cached_property
    def _position_by_shard(self) -> tuple[int, ...]:
        """Return a dense shard index to committee position table.

        Returns:
            tuple[int, ...]: Committee position for each shard index.
        """
        table = [-1] * self.n_shards
        for position, member in enumerate(self.members):
            for shard in member.shard_indices:
                table[shard] = position
        return tuple(table)

    def position_of(self, *, node_id: str) -> int:
        """Return a node's position within the committee ordering.

        Args:
            node_id (str): On-chain staking pool object ID to locate.

        Returns:
            int: Zero-based position within ``members``.

        Raises:
            KeyError: If no member carries the given node ID.
        """
        position = self._position_by_node_id.get(node_id)
        if position is None:
            raise KeyError(f"Node {node_id} is not in the epoch {self.epoch} committee")
        return position

    def position_for_shard(self, *, shard_index: int) -> int:
        """Return the committee position of the node holding a shard.

        Args:
            shard_index (int): Zero-based shard index.

        Returns:
            int: Zero-based committee position of the holding node.

        Raises:
            ValueError: If ``shard_index`` is outside ``range(n_shards)``.
        """
        if shard_index < 0 or shard_index >= self.n_shards:
            raise ValueError(
                f"Shard index {shard_index} outside range(0, {self.n_shards})"
            )
        return self._position_by_shard[shard_index]

    def member_for_shard(self, *, shard_index: int) -> WalrusCommitteeMember:
        """Return the committee member holding a shard.

        Args:
            shard_index (int): Zero-based shard index.

        Returns:
            WalrusCommitteeMember: The member responsible for the shard.

        Raises:
            ValueError: If ``shard_index`` is outside ``range(n_shards)``.
        """
        return self.members[self.position_for_shard(shard_index=shard_index)]


def pack_signers_bitmap(
    *, signer_positions: Iterable[int], committee_size: int
) -> bytes:
    """Pack committee positions into a Walrus ``signers_bitmap``.

    Allocates ``ceil(committee_size / 8)`` zero bytes, then sets bit
    ``position % 8`` of byte ``position // 8`` for each signer, LSB-first within
    each byte. This matches the on-chain encoding consumed by ``certify_blob``.

    Args:
        signer_positions (Iterable[int]): Zero-based POSITIONS WITHIN THE
            COMMITTEE ORDERING of the nodes that signed. These are not node IDs
            and not shard indices.
        committee_size (int): Number of committee members. Determines the bitmap
            length; ``n_shards`` is not a substitute.

    Returns:
        bytes: The packed bitmap.

    Raises:
        ValueError: If ``committee_size`` is negative, or a signer position
            falls outside ``range(committee_size)``.
    """
    if committee_size < 0:
        raise ValueError(f"committee_size must be non-negative, got {committee_size}")
    bitmap = bytearray((committee_size + 7) // 8)
    for position in signer_positions:
        if position < 0 or position >= committee_size:
            raise ValueError(
                f"Signer position {position} outside committee of size {committee_size}"
            )
        bitmap[position // 8] |= 1 << (position % 8)
    return bytes(bitmap)


def unpack_signers_bitmap(*, bitmap: bytes, committee_size: int) -> tuple[int, ...]:
    """Unpack a Walrus ``signers_bitmap`` into committee positions.

    Inverse of :func:`pack_signers_bitmap`. Not required by the upload flow, but
    it makes the round-trip property directly unit-testable and materially helps
    when diagnosing a rejected certification.

    Args:
        bitmap (bytes): Packed bitmap as produced by ``pack_signers_bitmap``.
        committee_size (int): Number of committee members. Bits at positions at
            or beyond this value are padding and are ignored.

    Returns:
        tuple[int, ...]: Ascending signer positions.

    Raises:
        ValueError: If ``committee_size`` is negative, or ``bitmap`` is shorter
            than ``committee_size`` requires.
    """
    if committee_size < 0:
        raise ValueError(f"committee_size must be non-negative, got {committee_size}")
    expected_length = (committee_size + 7) // 8
    if len(bitmap) < expected_length:
        raise ValueError(
            f"Bitmap of {len(bitmap)} bytes too short for committee of size "
            f"{committee_size} (expected at least {expected_length})"
        )
    return tuple(
        position
        for position in range(committee_size)
        if bitmap[position // 8] & (1 << (position % 8))
    )


def protobuf_json_to_python(*, value: object) -> JsonValue:
    """Convert a protobuf ``Value`` object rendering into plain Python data.

    This is the only place in the committee path that touches the protobuf
    ``Value`` shape returned by ``GetDynamicFields``. Everything downstream
    works with plain dicts, lists, strings and numbers, so a change in the wire
    rendering has one place to be fixed. (Note that ``pytusk.tusky.tusky_cmds``
    still walks protobuf structures by hand in its blob-inspection helpers; that
    code predates this function and has not been migrated onto it.)

    ASSUMPTION, load-bearing and unenforced: the protobuf runtime returns
    ``None`` for an UNSET oneof member. That is betterproto's behaviour, which
    is what pysui uses. A runtime returning field defaults instead -- stock
    ``google.protobuf`` returns ``""`` for an unset ``string_value`` -- would
    make every value decode as the empty string, because the first check below
    would match unconditionally.

    Args:
        value (object): A protobuf ``Value`` (or ``None``). Accessed by
            attribute name so this module need not import the protobuf types.

    Returns:
        JsonValue: Plain Python representation of the value.
    """
    string_value = getattr(value, "string_value", None)
    if string_value is not None:
        return string_value
    number_value = getattr(value, "number_value", None)
    if number_value is not None:
        return number_value
    bool_value = getattr(value, "bool_value", None)
    if bool_value is not None:
        return bool_value
    struct_value = getattr(value, "struct_value", None)
    if struct_value is not None:
        return {
            name: protobuf_json_to_python(value=field)
            for name, field in struct_value.fields.items()
        }
    list_value = getattr(value, "list_value", None)
    if list_value is not None:
        return [protobuf_json_to_python(value=entry) for entry in list_value.values]
    return None


def _require_dict(*, node: JsonValue, path: str) -> dict[str, JsonValue]:
    """Return ``node`` as a mapping or raise with the on-chain path.

    Args:
        node (JsonValue): Decoded value to check.
        path (str): Human-readable location, used in the error message.

    Returns:
        dict[str, JsonValue]: The mapping.

    Raises:
        TypeError: If ``node`` is not a mapping.
    """
    if not isinstance(node, dict):
        raise TypeError(f"Expected a mapping at {path}, got {type(node).__name__}")
    return node


def _require_list(*, node: JsonValue, path: str) -> list[JsonValue]:
    """Return ``node`` as a list or raise with the on-chain path.

    Args:
        node (JsonValue): Decoded value to check.
        path (str): Human-readable location, used in the error message.

    Returns:
        list[JsonValue]: The list.

    Raises:
        TypeError: If ``node`` is not a list.
    """
    if not isinstance(node, list):
        raise TypeError(f"Expected a list at {path}, got {type(node).__name__}")
    return node


def _require_str(*, node: JsonValue, path: str) -> str:
    """Return ``node`` as a string or raise with the on-chain path.

    Args:
        node (JsonValue): Decoded value to check.
        path (str): Human-readable location, used in the error message.

    Returns:
        str: The string.

    Raises:
        TypeError: If ``node`` is not a string.
    """
    if not isinstance(node, str):
        raise TypeError(f"Expected a string at {path}, got {type(node).__name__}")
    return node


def _require_int(*, node: JsonValue, path: str) -> int:
    """Return ``node`` as an int or raise with the on-chain path.

    On-chain integers arrive through the JSON rendering as floats (``489.0``),
    so this narrows them back to ``int``. Booleans are rejected outright rather
    than silently coerced to 0/1.

    Args:
        node (JsonValue): Decoded value to check.
        path (str): Human-readable location, used in the error message.

    Returns:
        int: The integer value.

    Raises:
        TypeError: If ``node`` is not a non-boolean number.
        ValueError: If ``node`` is a float with a fractional part.
    """
    if isinstance(node, bool) or not isinstance(node, (int, float)):
        raise TypeError(f"Expected a number at {path}, got {type(node).__name__}")
    if isinstance(node, float) and not node.is_integer():
        raise ValueError(f"Expected an integer at {path}, got {node}")
    return int(node)


def staking_epoch(*, staking_inner: dict[str, JsonValue]) -> int:
    """Return the current Walrus epoch from a decoded ``StakingInnerV1``.

    Args:
        staking_inner (dict[str, JsonValue]): Decoded ``StakingInnerV1`` value.

    Returns:
        int: Current Walrus epoch.

    Raises:
        TypeError: If the epoch field is missing or malformed.
    """
    return _require_int(node=staking_inner.get("epoch"), path="StakingInnerV1.epoch")


def staking_n_shards(*, staking_inner: dict[str, JsonValue]) -> int:
    """Return the total shard count from a decoded ``StakingInnerV1``.

    Args:
        staking_inner (dict[str, JsonValue]): Decoded ``StakingInnerV1`` value.

    Returns:
        int: Total number of shards for the epoch.

    Raises:
        TypeError: If the field is missing or malformed.
    """
    return _require_int(
        node=staking_inner.get("n_shards"), path="StakingInnerV1.n_shards"
    )


def staking_pools_table_id(*, staking_inner: dict[str, JsonValue]) -> str:
    """Return the object ID of the ``pools`` ObjectTable.

    The table holds a ``StakingPool`` per REGISTERED node, which is a superset
    of the current committee -- callers must filter by committee membership
    rather than treating every pool entry as a committee member.

    Args:
        staking_inner (dict[str, JsonValue]): Decoded ``StakingInnerV1`` value.

    Returns:
        str: Object ID of the pools table, usable as a dynamic-field parent.

    Raises:
        TypeError: If the field is missing or malformed.
    """
    pools = _require_dict(node=staking_inner.get("pools"), path="StakingInnerV1.pools")
    return _require_str(node=pools.get("id"), path="StakingInnerV1.pools.id")


def staking_pools_size(*, staking_inner: dict[str, JsonValue]) -> int:
    """Return the number of entries the pools table declares it holds.

    ``ObjectTable`` records its own entry count, so the expected size is known
    before the table is enumerated. Comparing it against what a fetch returns is
    what distinguishes an empty result caused by transport from a table that is
    genuinely empty.

    Args:
        staking_inner (dict[str, JsonValue]): Decoded ``StakingInnerV1``
            contents.

    Returns:
        int: The declared entry count.

    Raises:
        TypeError: If the expected structure is absent.
        ValueError: If the declared size is not an integer.
    """
    pools = _require_dict(node=staking_inner.get("pools"), path="pools")
    size = pools.get("size")
    if isinstance(size, str):
        try:
            return int(size)
        except ValueError as exc:
            raise ValueError(f"pools.size is not an integer: {size!r}") from exc
    return _require_int(node=size, path="pools.size")


def staking_committee_shards(
    *, staking_inner: dict[str, JsonValue]
) -> tuple[tuple[str, tuple[int, ...]], ...]:
    """Return the committee as ordered ``(node_id, shard_indices)`` pairs.

    The on-chain ``Committee`` is a ``VecMap`` rendered as
    ``committee.pos0.contents`` -- a LIST of ``{key, value}`` entries. Its order
    is the committee ordering that ``signers_bitmap`` indexes, so the list order
    is preserved exactly and never re-sorted or converted to a mapping.

    Args:
        staking_inner (dict[str, JsonValue]): Decoded ``StakingInnerV1`` value.

    Returns:
        tuple[tuple[str, tuple[int, ...]], ...]: Ordered committee entries.

    Raises:
        TypeError: If the committee structure is not as expected.
    """
    committee = _require_dict(
        node=staking_inner.get("committee"), path="StakingInnerV1.committee"
    )
    vec_map = _require_dict(node=committee.get("pos0"), path="committee.pos0")
    contents = _require_list(node=vec_map.get("contents"), path="committee.contents")
    entries: list[tuple[str, tuple[int, ...]]] = []
    for position, raw_entry in enumerate(contents):
        entry = _require_dict(node=raw_entry, path=f"committee.contents[{position}]")
        node_id = _require_str(
            node=entry.get("key"), path=f"committee.contents[{position}].key"
        )
        shard_values = _require_list(
            node=entry.get("value"), path=f"committee.contents[{position}].value"
        )
        shard_indices = tuple(
            _require_int(
                node=shard, path=f"committee.contents[{position}].value[{offset}]"
            )
            for offset, shard in enumerate(shard_values)
        )
        entries.append((node_id, shard_indices))
    return tuple(entries)


def pool_node_id(*, pool: dict[str, JsonValue]) -> str:
    """Return the storage node identifier recorded on a staking pool.

    Args:
        pool (dict[str, JsonValue]): Decoded ``StakingPool`` contents.

    Returns:
        str: The node identifier, matching the pools table entry key.

    Raises:
        TypeError: If the expected structure is absent.
    """
    node_info = _require_dict(node=pool.get("node_info"), path="node_info")
    return _require_str(node=node_info.get("node_id"), path="node_info.node_id")


def pool_network_address(*, pool: dict[str, JsonValue]) -> str:
    """Return the ``host:port`` address a storage node serves slivers on.

    Args:
        pool (dict[str, JsonValue]): Decoded ``StakingPool`` contents.

    Returns:
        str: The network address string.

    Raises:
        TypeError: If the expected structure is absent.
    """
    node_info = _require_dict(node=pool.get("node_info"), path="node_info")
    return _require_str(
        node=node_info.get("network_address"), path="node_info.network_address"
    )


def pool_public_key(*, pool: dict[str, JsonValue]) -> bytes:
    """Return the BLS12-381 public key of a storage node.

    The on-chain form is ``Element<UncompressedG1>`` -- 96 raw bytes, not the
    48 byte compressed encoding. Do not confuse this with
    ``node_info.network_public_key``, which is a 33 byte transport key serving
    an unrelated purpose.

    Args:
        pool (dict[str, JsonValue]): Decoded ``StakingPool`` contents.

    Returns:
        bytes: The 96 byte uncompressed G1 public key.

    Raises:
        TypeError: If the expected structure is absent.
        ValueError: If the decoded key is not 96 bytes.
    """
    node_info = _require_dict(node=pool.get("node_info"), path="node_info")
    public_key = _require_dict(
        node=node_info.get("public_key"), path="node_info.public_key"
    )
    encoded = _require_str(
        node=public_key.get("bytes"), path="node_info.public_key.bytes"
    )
    decoded = base64.b64decode(encoded)
    if len(decoded) != _UNCOMPRESSED_G1_LENGTH:
        raise ValueError(
            f"node_info.public_key.bytes decoded to {len(decoded)} bytes, "
            f"expected {_UNCOMPRESSED_G1_LENGTH}"
        )
    return decoded


def build_committee(
    *,
    staking_inner: dict[str, JsonValue],
    pools_by_node_id: dict[str, dict[str, JsonValue]],
) -> WalrusCommittee:
    """Assemble the active committee from staking inner state and pool data.

    Committee ordering comes from ``staking_inner`` and is preserved exactly --
    a member's index in :attr:`WalrusCommittee.members` is its committee
    position, which is what ``signers_bitmap`` indexes. ``pools_by_node_id`` is
    consulted only as a lookup; the pools table is a superset of the committee
    and its own ordering carries no meaning here.

    Shard coverage is validated: every shard in ``range(n_shards)`` must be held
    by exactly one member. An uncovered or doubly-claimed shard would otherwise
    surface much later as an upload that hangs or a certification that cannot
    reach quorum.

    Args:
        staking_inner (dict[str, JsonValue]): Decoded ``StakingInnerV1``
            contents.
        pools_by_node_id (dict[str, dict[str, JsonValue]]): Staking pool
            contents keyed by node identifier.

    Returns:
        WalrusCommittee: The assembled committee.

    Raises:
        KeyError: If a committee member has no corresponding pool entry.
        TypeError: If the decoded structures are not as expected.
        ValueError: If a shard index is out of range, or the shard assignment
            does not cover every shard exactly once.
    """
    n_shards = staking_n_shards(staking_inner=staking_inner)
    members: list[WalrusCommitteeMember] = []
    coverage = [0] * n_shards
    for node_id, shard_indices in staking_committee_shards(staking_inner=staking_inner):
        pool = pools_by_node_id.get(node_id)
        if pool is None:
            raise KeyError(
                f"Committee member {node_id} has no entry in the pools table"
            )
        for shard in shard_indices:
            if shard < 0 or shard >= n_shards:
                raise ValueError(
                    f"Committee member {node_id} claims shard {shard}, outside "
                    f"range(0, {n_shards})"
                )
            coverage[shard] += 1
        members.append(
            WalrusCommitteeMember(
                node_id=node_id,
                shard_indices=shard_indices,
                network_address=pool_network_address(pool=pool),
                public_key=pool_public_key(pool=pool),
            )
        )
    uncovered = [shard for shard, count in enumerate(coverage) if count == 0]
    duplicated = [shard for shard, count in enumerate(coverage) if count > 1]
    if uncovered or duplicated:
        raise ValueError(
            f"Shard assignment does not cover every shard exactly once: "
            f"{len(uncovered)} uncovered (first: {uncovered[:5]}), "
            f"{len(duplicated)} duplicated (first: {duplicated[:5]})"
        )
    return WalrusCommittee(
        epoch=staking_epoch(staking_inner=staking_inner),
        n_shards=n_shards,
        members=tuple(members),
    )


async def fetch_staking_inner(
    *, reader: ChainReader, staking_object: str
) -> dict[str, JsonValue]:
    """Fetch and decode the ``StakingInnerV1`` dynamic field of the staking object.

    Args:
        reader (ChainReader): Chain transport to read through.
        staking_object (str): Object ID of the Walrus staking object.

    Returns:
        dict[str, JsonValue]: The decoded ``StakingInnerV1`` value, carrying
        ``epoch``, ``n_shards``, ``committee`` and ``pools``.

    Raises:
        RuntimeError: If the dynamic fields cannot be fetched, the staking
            object exposes none, or no field carries the supported staking
            inner type.
        TypeError: If the decoded field is not as expected.
    """
    result = await reader.execute(command=GetDynamicFields(object_id=staking_object))
    if not result.is_ok():
        raise RuntimeError(f"Cannot get staking dynamic fields: {result.result_string}")
    dynamic_fields = result.result_data.dynamic_fields
    if not dynamic_fields:
        raise RuntimeError(f"Staking object {staking_object} has no dynamic fields")
    for candidate in dynamic_fields:
        if (candidate.value_type or "").endswith(_STAKING_INNER_TYPE_SUFFIX):
            selected = candidate
            break
    else:
        found = ", ".join(
            sorted({entry.value_type or "<unknown>" for entry in dynamic_fields})
        )
        raise RuntimeError(
            f"Staking object {staking_object} exposes no dynamic field ending in "
            f"{_STAKING_INNER_TYPE_SUFFIX}; found: {found}"
        )
    decoded = protobuf_json_to_python(value=selected.field_object.json)
    field = _require_dict(node=decoded, path="staking dynamic field")
    return _require_dict(node=field.get("value"), path="StakingInnerV1")


async def fetch_pools(
    *,
    reader: ChainReader,
    pools_table_id: str,
    expected_size: int | None = None,
) -> dict[str, dict[str, JsonValue]]:
    """Fetch and decode every staking pool held in the pools table.

    The pools table is a superset of the active committee: it retains entries
    for nodes that never joined and for nodes that have since left. Callers
    must filter it by the committee's node identifiers rather than treating it
    as the committee itself.

    All pages are collected -- the table routinely exceeds a single page.

    Args:
        reader (ChainReader): Chain transport to read through.
        pools_table_id (str): Object identifier of the ``pools`` table, as
            returned by :func:`staking_pools_table_id`.
        expected_size (int | None): Entry count the table declares, as returned
            by :func:`staking_pools_size`. When supplied, a mismatch against the
            number of entries actually fetched raises rather than silently
            yielding a partial committee.

    Returns:
        dict[str, dict[str, JsonValue]]: Decoded ``StakingPool`` contents keyed
        by storage node identifier.

    Raises:
        RuntimeError: If the pools dynamic fields cannot be fetched, or the
            number fetched does not match ``expected_size``.
        TypeError: If a decoded pool is not as expected.
    """
    result = await reader.execute_for_all(
        command=GetDynamicFields(object_id=pools_table_id)
    )
    if not result.is_ok():
        raise RuntimeError(f"Cannot get pools dynamic fields: {result.result_string}")
    pools: dict[str, dict[str, JsonValue]] = {}
    for entry in result.result_data.dynamic_fields:
        decoded = protobuf_json_to_python(value=entry.child_object.json)
        pool = _require_dict(node=decoded, path="staking pool")
        pools[pool_node_id(pool=pool)] = pool
    if expected_size is not None and len(pools) != expected_size:
        raise RuntimeError(
            f"Pools table {pools_table_id} declares {expected_size} entries "
            f"but {len(pools)} were fetched; refusing to build a partial "
            f"committee."
        )
    return pools


async def fetch_epoch(*, reader: ChainReader, staking_object: str) -> int:
    """Fetch the current Walrus epoch.

    A single read. This is deliberately cheaper than :func:`fetch_committee`,
    which makes it the right validator for any epoch-keyed cache a caller
    builds on top.

    Args:
        reader (ChainReader): Chain transport to read through.
        staking_object (str): Object ID of the Walrus staking object.

    Returns:
        int: Current Walrus epoch.

    Raises:
        RuntimeError: If the on-chain read fails.
        TypeError: If the decoded structure is not as expected.
    """
    staking_inner = await fetch_staking_inner(
        reader=reader, staking_object=staking_object
    )
    return staking_epoch(staking_inner=staking_inner)


async def fetch_committee(
    *, reader: ChainReader, staking_object: str
) -> WalrusCommittee:
    """Fetch the active Walrus storage committee from chain.

    Two reads are issued: the staking object's ``StakingInnerV1`` field, which
    supplies the epoch, shard count and committee ordering, and the pools table,
    which supplies each member's network address and public key. The second is
    paged and is the most expensive read in the SDK -- budget for it.

    No result is cached; see the module docstring for why, and for what to do
    instead if per-call cost becomes a problem.

    Args:
        reader (ChainReader): Chain transport to read through.
        staking_object (str): Object ID of the Walrus staking object.

    Returns:
        WalrusCommittee: The committee for the current epoch.

    Raises:
        KeyError: If a committee member has no entry in the pools table.
        RuntimeError: If either on-chain read fails, or the pools table
            returns a different number of entries than it declares.
        TypeError: If the decoded structures are not as expected.
    """
    staking_inner = await fetch_staking_inner(
        reader=reader, staking_object=staking_object
    )
    pools_by_node_id = await fetch_pools(
        reader=reader,
        pools_table_id=staking_pools_table_id(staking_inner=staking_inner),
        expected_size=staking_pools_size(staking_inner=staking_inner),
    )
    return build_committee(
        staking_inner=staking_inner, pools_by_node_id=pools_by_node_id
    )
