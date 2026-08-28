#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Presentation helpers for the tusky CLI.

PRESENTATION ONLY. Everything here turns values that have already been
computed into strings a human or a JSON consumer reads. Nothing here queries
a client, parses chain state, or decides anything about what a command does
-- argument and config plumbing stays in
:mod:`pytusk.tusky.tusky_cmds_common`, and domain logic belongs in the
library proper, outside ``tusky/`` entirely.

That division is the placement rule from Plan #28 step 10: the consumer
determines the module. A helper an SDK user would also call is not CLI
presentation and does not live here.
"""

__all__ = [
    "format_token_amount",
]


def format_token_amount(*, raw: int, decimals: int) -> str:
    """Render a raw integer token amount as an exact decimal string.

    Uses integer ``divmod`` rather than floating-point division, so the
    result is exact for any magnitude -- important here since raw amounts
    (MIST, FROST) can run into the billions and a float division could lose
    precision at that range.

    Args:
        raw (int): The raw integer amount (may be negative).
        decimals (int): Number of decimal places the token uses.

    Returns:
        str: Exact decimal string rendering, e.g. ``"0.004603480"`` for
            ``raw=4603480, decimals=9``.
    """
    sign = "-" if raw < 0 else ""
    divisor = 10**decimals
    whole, frac = divmod(abs(raw), divisor)
    return f"{sign}{whole}.{frac:0{decimals}d}"
