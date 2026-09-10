#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Sui object-ID to raw-bytes conversion.

A single, narrow conversion: a Sui object ID (``0x`` followed by 64 hex
characters) to its 32 raw bytes. This is a DIFFERENT identifier and a
DIFFERENT encoding from a Walrus blob ID or root hash -- see
:mod:`pytusk.core.encoding.redstuff` for those. Do not conflate the two.
"""

__all__ = [
    "object_id_to_raw_bytes",
]


def object_id_to_raw_bytes(*, object_id: str) -> bytes:
    """Convert a Sui object ID string into its 32 raw bytes.

    Sui object IDs are ``0x`` followed by 64 hex characters (32 bytes).
    This is a DIFFERENT identifier and a DIFFERENT encoding from the Walrus
    blob ID -- do not conflate the two. It is needed for the deletable-blob
    branch of :func:`~pytusk.core.certification.confirmation_message`'s
    ``object_id`` argument: the SIGNED MESSAGE requires the raw bytes, while
    the ``ReadStorageConfirmation`` URL path
    (:class:`~pytusk.commands.node_commands.ReadStorageConfirmation`) takes
    the same object ID as the ``0x...`` string, unconverted. See
    :mod:`pytusk.core.native_upload`'s module docstring's four-row table for
    the complete set of 32-byte identifier encodings in play across native
    upload and how they differ.

    Args:
        object_id (str): A Sui object ID, ``0x`` followed by 64 hex
            characters.

    Returns:
        bytes: The 32 raw bytes the object ID encodes.

    Raises:
        ValueError: If ``object_id`` is not well-formed hex, or does not
            decode to exactly 32 bytes.
    """
    text = object_id[2:] if object_id.startswith(("0x", "0X")) else object_id
    try:
        decoded = bytes.fromhex(text)
    except ValueError as exc:
        raise ValueError(f"object_id {object_id!r} is not valid hex") from exc
    if len(decoded) != 32:
        raise ValueError(
            f"object_id {object_id!r} decoded to {len(decoded)} bytes, expected 32"
        )
    return decoded
