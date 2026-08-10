Introducing pytusk
===================

``pytusk`` is a Python SDK for interacting with `Walrus
<https://docs.walrus.site>`_ decentralized storage on Sui. It provides an
async client for storing and reading blobs and quilts, and for managing
their lifecycle on-chain.

Relationship to pysui
----------------------

``pytusk`` does not implement its own Sui-level configuration or
transport — it builds on top of `pysui
<https://github.com/FrankC01/pysui>`_ for both:

- :py:class:`~pytusk.PytuskConfiguration` wraps a pysui
  ``PysuiConfiguration``, adding Walrus-specific settings (aggregator and
  publisher endpoints, Walrus binary path) alongside the Sui network,
  address, and keystore configuration pysui already manages.
- :py:class:`~pytusk.WalrusClient` delegates all Sui-level operations —
  transaction building, execution, and querying — to the underlying
  pysui client. ``pytusk`` only adds the Walrus HTTP layer (aggregator/
  publisher requests) and the Walrus-specific move calls on top.

If you're already familiar with pysui's configuration and client model,
most of that knowledge carries over directly.

Client Design
--------------

``WalrusClient`` follows the same command-pattern style as pysui: a
single ``execute()`` entry point dispatches a command object rather than
exposing one method per operation.

- :py:class:`~pytusk.WalrusCommand` subclasses (e.g.
  :py:class:`~pytusk.ReadBlob`, :py:class:`~pytusk.StoreBlob`) are
  dispatched over HTTP to the Walrus aggregator or publisher, depending
  on the command.
- Sui-level commands (pysui ``SuiCommand`` subclasses) are forwarded
  directly to the underlying pysui client.
- ``await client.transaction(**kwargs)`` returns a pysui PTB builder for
  constructing custom Move calls — see :doc:`transactions` for the
  Walrus-specific PTBs ``pytusk`` builds internally.

Next Steps
-----------

See :doc:`installation` to get set up, or :doc:`configuration` for how
``PytuskConfiguration`` and pysui's ``PysuiConfiguration`` line up.
