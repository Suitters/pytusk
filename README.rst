===================================================
pytusk - Walrus SDK (BETA)
===================================================

A python SDK for building interactions with Walrus storage management.

Installing ``pytusk`` automatically installs the ``pysui`` SDK for underlying
configuration and transport support.

Requires **Python 3.10.6 or later**.

*****************
Installation
*****************

.. code-block:: bash

    pip install pytusk

Any Python package manager (pipenv, poetry, uv, etc.) works equally well in
place of ``pip``.

*****************
Quick start
*****************

-------------------
Configuration setup
-------------------

As noted, ``pytusk`` leverages ``pysui`` for configuration and client transport
support. So when setting up to interact with Walrus the configuration setup is
first step::

    from pytusk import PytuskConfiguration
    from pytusk.client.walrus_client import WalrusClient

    cfg = PytuskConfiguration(active_network="testnet", pysui_profile_name="testnet")
    client = WalrusClient(pytusk_config=cfg)

``active_network`` and ``pysui_profile_name`` must both be set, and to the same
network — ``pytusk`` does not infer one from the other, and Walrus is not
available on devnet.

See the full documentation for configuration options, available commands, and
transaction examples.

-------------------
tusky CLI
-------------------

``pytusk`` also installs the ``tusky`` command-line tool, for storing, reading,
and managing the lifecycle of Walrus blobs and quilts without writing any code::

    tusky --help

*****************
Documentation
*****************

Full documentation (configuration, commands, transactions, and the tusky CLI
reference) is available at https://pytusk.readthedocs.io/en/latest/index.html.
