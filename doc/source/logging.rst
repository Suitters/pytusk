Native Upload Logging
======================

The native upload pipeline (:func:`~pytusk.core.native_upload.store_blob_native`,
:func:`~pytusk.core.native_upload.certify`, committee resolution in
:mod:`pytusk.core.committee`) reports progress through the standard
library ``logging`` module rather than by printing directly. This page
documents the logger names, what they emit, and how to attach a handler
— both from ``tusky`` and from library code calling pytusk directly.

Logger Names
-------------

Every pytusk module gets its own logger via ``logging.getLogger(__name__)``,
so the three loggers relevant to native upload progress are:

``pytusk.core.native_upload.fanout``
   Emits ``INFO``-level records for: per-node sliver-upload outcomes
   (success, failure with reason, cancelled stragglers), and sliver
   fan-out start/summary and periodic heartbeat progress lines during the
   fan-out stage. Emits ``WARNING``-level records for individual
   storage-node failures that are *tolerated* because quorum is still
   reachable from the rest of the committee (dead DNS, expired/self-signed
   TLS certs, and similar node-side issues are common on testnet and do
   not by themselves fail the upload).

``pytusk.core.native_upload.confirm``
   Emits ``INFO``-level records for confirmation-collection progress
   (quorum reached, grace window, per-node confirmation outcomes) and its
   own periodic heartbeat progress lines.

``pytusk.core.committee``
   Emits ``INFO``-level records for committee/storage-pool resolution:
   ``GetDynamicFields`` page counts when reading the staking object and
   pools table.

All three loggers propagate up to the ``pytusk`` logger by Python's normal
logger-hierarchy rules — attaching a handler to ``pytusk`` (rather than
to each submodule individually) is the simplest way to capture
everything.

Using tusky's Built-in Flags
------------------------------

``store_blob_native`` and ``certify_blob`` accept ``--log-file PATH``
and/or ``--verbose``:

.. code-block:: console

   tusky store_blob_native --file blob.bin --epochs 5 --mode execute \
                            --log-file upload.log --verbose

``--log-file PATH``
   Adds an ``INFO``-level file handler at exactly ``PATH`` — no default
   or derived path. Written even if the run fails partway through.

``--verbose``
   Adds an ``INFO``-level stream handler to stdout, with stdout
   reconfigured for line buffering so progress is visible live even when
   output is redirected to a file.

Neither is enabled by default; passing neither means the pipeline runs
silently except for its final JSON receipt (or error) on stdout/stderr.
Both can be combined, and the setup is idempotent — safe to configure
once per process.

Attaching Your Own Handler
-----------------------------

Library code calling :mod:`pytusk.core.native_upload` or
:mod:`pytusk.core.committee` directly (not through ``tusky``) attaches a
handler the same way as any other Python logger:

.. code-block:: python

   import logging

   logger = logging.getLogger("pytusk")
   logger.setLevel(logging.INFO)
   logger.addHandler(logging.StreamHandler())

   # ... call store_blob_native() / certify() / committee resolution ...

No handler is configured by pytusk itself outside of ``tusky``'s opt-in
setup — a library consumer that adds no handler sees no output, per
Python logging's standard "library code should not configure logging"
convention.
