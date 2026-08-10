Installation
============

``pytusk`` requires **Python 3.10.6 or later**. It depends on `pysui
<https://github.com/FrankC01/pysui>`_ for Sui-level configuration and
transport, which is installed automatically as a dependency.

Setup
-----

Step 1: Install pytusk
~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   pip install pytusk

Any Python package manager (pipenv, poetry, uv, etc.) works equally well
in place of ``pip``.

Step 2: First time configuration
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``pytusk`` loads its configuration through :py:class:`PytuskConfiguration`,
which wraps a ``pysui`` :py:class:`PysuiConfiguration`. See
:doc:`configuration` for how the two line up and how to create your first
configuration file.

Step 3: Test install
~~~~~~~~~~~~~~~~~~~~~

Verify the install by importing pytusk:

.. code-block:: bash

   python -c "import pytusk; print(pytusk.__version__)"

Verify the ``tusky`` CLI is installed and reachable:

.. code-block:: bash

   tusky --help
