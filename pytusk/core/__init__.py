#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Core Walrus protocol modules.

Domain logic for the Walrus protocol, layered bottom-up:

- :mod:`pytusk.core.types` -- shared result, outcome, error, and
  tip-config types (LEAF, client-free).
- :mod:`pytusk.core.encoding` -- RedStuff erasure-coding size math and
  blob-ID/root-hash/object-ID byte conversions (client-free).
- :mod:`pytusk.core.chain` -- client-free on-chain state helpers:
  committee/epoch resolution, transaction-effects inspection, Blob-object
  field extraction, and WAL coin-type matching.
- :mod:`pytusk.core.certification` -- BLS confirmation and certificate
  handling (pure/local -- no network calls).
- :mod:`pytusk.core.ops` -- PTB composition (``*_compose``) and
  client-driven execution/reads (``*_execute``, ``*_reads``) against the
  Walrus contracts.
- :mod:`pytusk.core.native_upload` -- native upload orchestration:
  per-node sliver fan-out, confirmation collection, and certification.
- :mod:`pytusk.core.relay_upload` -- relay upload orchestration: tip
  computation, upload-to-relay, and relay certificate parsing.
- :mod:`pytusk.core.pipelines` -- the top composition layer
  (``store_blob_native``, ``store_blob_relay``) that runs the stages
  above, in order, through a :class:`~pytusk.client.walrus_client.WalrusClient`.

The Walrus HTTP client itself lives in :mod:`pytusk.client`.
"""
