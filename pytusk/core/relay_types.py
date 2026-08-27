#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Value types shared by the relay HTTP layer and relay orchestration.

These live OUTSIDE :mod:`pytusk.core.relay_upload` deliberately. The
commands in :mod:`pytusk.commands.relay_commands` need them, and
orchestration in :mod:`pytusk.core.relay_upload` imports those commands --
so defining them inside that package would make the two import each other.
Orchestration depending on transport is the correct direction; transport
depending on orchestration is not.

They are pure data with no behaviour beyond deriving from their own fields,
which is what makes them safe to share across that boundary.
"""

from __future__ import annotations

import dataclasses
import enum


@dataclasses.dataclass(kw_only=True, frozen=True)
class ConstTip:
    """A flat tip, charged per upload regardless of blob size.

    Mirrors Walrus's ``TipKind::Const(u64)``, whose wire form is serde's
    externally tagged ``{"const": <amount>}``.

    Attributes:
        amount (int): Tip in MIST, charged unconditionally.
    """

    amount: int


@dataclasses.dataclass(kw_only=True, frozen=True)
class LinearTip:
    """A tip that scales with the ENCODED size of the blob.

    The tip is ``base + encoded_size_mul_per_kib * ceil(encoded_length /
    1024)``, where ``encoded_length`` is the fully RS-encoded blob length
    (per-shard metadata plus every shard's slivers) -- NOT the unencoded
    byte count the caller supplied. The unencoded length is only an INPUT
    to the encoded-length calculation.

    Walrus's own doc comment for this variant writes the rounding as a
    floor (``encoded_size_in_bytes / 1024``), but its implementation calls
    ``div_ceil(1024)``. The implementation is authoritative: pytusk rounds
    UP, so an encoded length of exactly 1024 bytes bills one KiB and 1025
    bytes bills two. Mirrors Walrus's ``TipKind::Linear``, whose wire form
    is ``{"linear": {"base": N, "encoded_size_mul_per_kib": N}}``.

    Attributes:
        base (int): Flat component in MIST, charged on every upload.
        encoded_size_mul_per_kib (int): MIST charged per KiB (rounded up)
            of encoded blob length, added to ``base``.
    """

    base: int
    encoded_size_mul_per_kib: int


# How a relay computes its tip. Mirrors Walrus's ``TipKind`` enum; the two
# members are exhaustive, so a match over them needs no fallback arm.
TipKind = ConstTip | LinearTip


@dataclasses.dataclass(kw_only=True, frozen=True)
class TipConfig:
    """A relay's advertised tipping policy, from ``GET /v1/tip-config``.

    Mirrors Walrus's ``TipConfig`` enum. Both Walrus enums are serde
    externally tagged with ``rename_all = "snake_case"``, so a relay that
    charges nothing serialises as the BARE JSON STRING ``"no_tip"`` -- not
    an object -- and is represented here with both fields ``None``. A relay
    that charges serialises as ``{"send_tip": {"address": ..., "kind":
    ...}}``.

    Attributes:
        address (str | None): Sui address the tip is transferred to, or
            ``None`` when the relay requires no tip.
        kind (TipKind | None): How the tip amount is computed, or ``None``
            when the relay requires no tip.
    """

    address: str | None
    kind: TipKind | None

    @property
    def requires_payment(self) -> bool:
        """Whether this relay requires a tip before it will accept an upload.

        Returns:
            bool: True when a tip must be paid, False for a ``no_tip`` relay.
        """
        return self.kind is not None


class RelayUploadOutcome(str, enum.Enum):
    """Terminal state of the POST stage alone.

    Deliberately NOT :class:`RelayOutcome`: the POST stage cannot occupy
    that enum's :attr:`RelayOutcome.CERTIFIED` (Tx2 has not run) or
    :attr:`RelayOutcome.NOT_STARTED` (Tx1 has) states, so reusing it would
    let a composable caller read a value the stage can never legitimately
    produce. :mod:`pytusk.core.relay_upload.pipeline` maps these onto
    :class:`RelayOutcome` for the encapsulated receipt.

    Attributes:
        UPLOADED: The relay returned a confirmation certificate.
        UNANSWERED: No answer within the attempt budget -- transport
            failures or 5xx. The paid tip is untouched and a later POST
            with the same tokens costs nothing further.
        REFUSED: The relay answered no (400/401/402). Resuming cannot
            help; a fixed paid tip cannot satisfy a 402.
    """

    __str__ = str.__str__

    UPLOADED = "uploaded"
    UNANSWERED = "unanswered"
    REFUSED = "refused"
