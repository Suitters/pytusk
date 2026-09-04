#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Result types for the on-chain Blob metadata (Walrus "attribute") read.

Mirrors the on-chain shape exactly: a Walrus ``Blob`` object carries at most
one ``metadata`` dynamic field, whose value is a Move ``Metadata`` struct
wrapping a ``VecMap<String, String>`` (``~/mysten_repos/walrus``,
``metadata.move``). :class:`Metadata` is one key/value pair from that map;
:class:`BlobMetadata` is the whole ordered collection, as read off chain by
:func:`~pytusk.core.chain.blob_metadata.fetch_blob_metadata`.

Deliberately absent: a mapping/dict return shape. ``VecMap`` preserves
insertion order and Walrus places no uniqueness burden beyond what the
on-chain type already enforces, so a list of pairs is the honest mirror of
the wire shape -- callers wanting dict-like lookup build one from
``BlobMetadata.data`` themselves rather than this module silently choosing
that projection for them.
"""

import dataclasses


@dataclasses.dataclass(kw_only=True, frozen=True)
class Metadata:
    """A single key/value pair from a Blob's on-chain metadata map.

    Attributes:
        key (str): The attribute key.
        value (str): The attribute value.
    """

    key: str
    value: str


@dataclasses.dataclass(kw_only=True, frozen=True)
class BlobMetadata:
    """The complete set of metadata key/value pairs for one Blob object.

    Attributes:
        data (list[Metadata]): Metadata entries, in on-chain ``VecMap``
            order.
    """

    data: list[Metadata]
