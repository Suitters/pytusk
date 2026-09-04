#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Coin-type predicates.

Client-free by construction: these operate on coin-type strings alone and
take no :class:`~pytusk.client.walrus_client.WalrusClient`, so they live
below ``pytusk/client/`` per the layering rule. ``matches_wal_coin_type``
moved here from ``pytusk.core.ops.coins`` at Plan #28's architect review:
``ops/coins.py`` imports ``WalrusClient``, so a below-client module
needing this predicate had to import it inside a function body to avoid
the cycle ``chain -> ops -> client -> chain``. Moving the function to its
correct layer removes the cycle instead of deferring it.
"""


def matches_wal_coin_type(*, coin_type: str, wal_coin_type: str) -> bool:
    """Check whether a coin_type string identifies the WAL coin.

    The single implementation. ``pytusk.tusky.tusky_cmds`` carried a
    duplicate of this helper until it was consolidated here: the one-way
    dependency rule forbids ``core`` importing ``tusky``, not ``tusky``
    importing ``core``, and ``tusky`` already imports from ``pytusk``
    freely. Uses an exact match against ``wal_coin_type`` when the active
    network has a pinned value (currently mainnet only); falls back to a
    substring match when unpinned (e.g. testnet, whose contracts are
    redeployed and don't have a stable package address to pin against).

    Args:
        coin_type (str): The coin_type string to check.
        wal_coin_type (str): The active network's pinned WAL coin type, or
            "" if unpinned.

    Returns:
        bool: True if coin_type identifies the WAL coin.
    """
    if wal_coin_type:
        return coin_type == wal_coin_type
    return "::wal::WAL" in coin_type
