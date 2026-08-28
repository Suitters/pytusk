#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Enforces client-free layering invariants below ``pytusk/client/``.

``pytusk.core.ops`` is deliberately split into COMPOSE modules
(``*_compose.py``: ``add_*`` functions plus pure validators, which append
move_calls to a caller-supplied ``AsyncSuiTransaction`` and never build,
sign, or submit anything) and EXECUTE modules (``*_execute.py``:
``execute_*`` functions, which open a transaction, delegate composition to
the matching ``add_*``, then build, sign, and submit via a client).

The load-bearing rule this file pins: A ``*_compose.py`` MODULE MUST NEVER
IMPORT OR REFERENCE A CLIENT / TRANSACTION-EXECUTOR. A compose function
takes a ``txn: AsyncSuiTransaction`` and contributes to it; it never
submits. This is what lets an SDK developer hold a transaction, choose its
own sender/sponsor, interleave their own move_calls, and decide
simulate-vs-execute for themselves -- a compose function that could reach a
client could also be tempted to submit on its own, quietly collapsing the
COMPOSE/EXECUTE split this package exists to enforce. This test parses each
``*_compose.py`` module's imports (via ``ast``, so it catches an import
anywhere in the file, including one nested inside a function body to break
a circular import) and fails if any of them names a client or
transaction-executor type.

The same client-free requirement also applies, for a different reason, to
``pytusk.core.types``, ``pytusk.core.encoding``, ``pytusk.core.chain``, and
``pytusk.core.certification``: these packages sit BELOW ``pytusk/client/``
in the project's layering (``client/walrus_client.py`` imports from
``core.chain``, and transitively from the others), so a client import in
any of them is not a style violation -- it is a real import cycle
(``chain -> client -> chain`` and friends). ``TestBelowClientModulesNeverImportAClient``
below runs the identical ``ast``-based check over that second set of
packages, reusing the same walker and forbidden-name/module constants as
the COMPOSE check above.
"""

import ast
from pathlib import Path

import pytest

_CORE_DIR = Path(__file__).resolve().parent.parent.parent / "pytusk" / "core"
_OPS_DIR = _CORE_DIR / "ops"

# Names that identify a client or transaction-executor. A compose module
# must reference none of these, anywhere -- not just at module scope --
# because reaching one is exactly what would let a compose function
# submit on its own, instead of merely contributing to a caller-owned
# transaction.
_FORBIDDEN_NAMES: frozenset[str] = frozenset(
    {
        "WalrusClient",
        "ExecuteTransaction",
    }
)

# Module paths that identify a client or transaction-executor. Importing
# the whole module (``import pytusk.client.walrus_client``) rather than a
# specific name from it is forbidden for the same reason as the names
# above.
_FORBIDDEN_MODULE_SUBSTRINGS: tuple[str, ...] = (
    "walrus_client",
)


def _compose_modules() -> list[Path]:
    """Return every ``*_compose.py`` module under ``pytusk.core.ops``."""
    return sorted(_OPS_DIR.glob("*_compose.py"))


def _below_client_modules() -> list[Path]:
    """Return every module in the packages that sit below ``pytusk/client/``.

    Covers ``pytusk.core.types``, ``pytusk.core.encoding``,
    ``pytusk.core.chain`` (every ``*.py`` file in each, including
    ``__init__.py``), plus the single module ``pytusk.core.certification``.
    ``client/walrus_client.py`` imports from ``core.chain``, so any of
    these packages importing a client back would be a genuine import
    cycle, not merely an inconsistency -- see this file's module
    docstring.
    """
    package_dirs = (
        _CORE_DIR / "types",
        _CORE_DIR / "encoding",
        _CORE_DIR / "chain",
    )
    modules = [
        module_path for package_dir in package_dirs for module_path in package_dir.glob("*.py")
    ]
    modules.append(_CORE_DIR / "certification.py")
    return sorted(modules)


def _imported_names_and_modules(*, source: str) -> tuple[set[str], set[str]]:
    """Collect every imported name and every imported module path.

    Walks the WHOLE syntax tree (``ast.walk``, not just top-level
    statements) so an import nested inside a function body -- e.g. a
    local import added to break a circular import between a compose and
    an execute module -- is caught exactly like a module-scope import.

    Args:
        source (str): The module's source code.

    Returns:
        tuple[set[str], set[str]]: (imported names, imported module paths).
            For ``import a.b.c``, the module path ``"a.b.c"`` is recorded.
            For ``from a.b import c``, both the module path ``"a.b"`` and
            the name ``"c"`` (or its ``as`` alias target's original name)
            are recorded.
    """
    tree = ast.parse(source)
    names: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module)
            for alias in node.names:
                names.add(alias.name)
    return names, modules


class TestComposeModulesNeverImportAClient:
    """No ``*_compose.py`` module may import or reference a client /
    transaction-executor -- see this file's module docstring for why."""

    @pytest.mark.parametrize("module_path", _compose_modules(), ids=lambda p: p.name)
    def test_no_forbidden_import(self, module_path: Path) -> None:
        """Fails if ``module_path`` imports a client/executor name or module.

        A failure here means a COMPOSE module has started depending on a
        client or transaction-executor -- e.g. someone added a
        ``client: WalrusClient`` parameter to an ``add_*`` function, or
        started calling ``ExecuteTransaction`` directly from a compose
        module. That collapses the COMPOSE/EXECUTE split this package
        exists to enforce: a compose function must only append to a
        caller-supplied ``AsyncSuiTransaction`` and never submit. Move the
        client-touching code into the matching ``*_execute.py`` module
        instead.
        """
        source = module_path.read_text()
        names, modules = _imported_names_and_modules(source=source)

        forbidden_names_found = names & _FORBIDDEN_NAMES
        assert not forbidden_names_found, (
            f"{module_path.name} imports {sorted(forbidden_names_found)}, "
            "which names a client/transaction-executor type. "
            "*_compose.py modules must never import or reference a client "
            "or transaction-executor -- a compose function takes a "
            "txn: AsyncSuiTransaction and contributes to it, it never "
            "submits. Move this import (and whatever uses it) to the "
            "matching *_execute.py module."
        )

        forbidden_modules_found = {
            module
            for module in modules
            if any(substring in module for substring in _FORBIDDEN_MODULE_SUBSTRINGS)
        }
        assert not forbidden_modules_found, (
            f"{module_path.name} imports from {sorted(forbidden_modules_found)}, "
            "a client/transaction-executor module. "
            "*_compose.py modules must never import or reference a client "
            "or transaction-executor -- a compose function takes a "
            "txn: AsyncSuiTransaction and contributes to it, it never "
            "submits. Move this import (and whatever uses it) to the "
            "matching *_execute.py module."
        )

    def test_at_least_two_compose_modules_are_checked(self) -> None:
        """Regression guard: the glob above must not silently match nothing.

        A typo in ``_OPS_DIR`` or the ``*_compose.py`` glob would make
        every parametrized case above vacuously pass -- this pins that the
        package actually has (at least) ``blob_compose.py`` and
        ``storage_compose.py`` for the parametrization to have found.
        """
        assert len(_compose_modules()) >= 2


class TestBelowClientModulesNeverImportAClient:
    """No module in ``pytusk.core.types``, ``pytusk.core.encoding``,
    ``pytusk.core.chain``, or ``pytusk.core.certification`` may import or
    reference a client / transaction-executor.

    These packages sit BELOW ``pytusk/client/`` in the project's layering:
    ``pytusk.client.walrus_client`` imports from ``pytusk.core.chain``, so
    a client import inside any of these packages closes a real import
    cycle (e.g. ``chain -> client -> chain``), not just a style
    inconsistency. This mirrors ``TestComposeModulesNeverImportAClient``
    above -- same walker, same forbidden-name/module constants -- applied
    to a different set of packages for a different (but related) reason.
    """

    @pytest.mark.parametrize(
        "module_path",
        _below_client_modules(),
        ids=lambda p: str(p.relative_to(_CORE_DIR)),
    )
    def test_no_forbidden_import(self, module_path: Path) -> None:
        """Fails if ``module_path`` imports a client/executor name or module.

        A failure here means a package below ``pytusk/client/`` has
        started depending on a client or transaction-executor -- e.g. a
        function-local import added to dodge a circular import instead of
        moving the function to the layer where it belongs. Move the
        client-touching code (or the function needing a client-free
        helper) to its correct layer instead of importing across the
        seam.
        """
        source = module_path.read_text()
        names, modules = _imported_names_and_modules(source=source)

        forbidden_names_found = names & _FORBIDDEN_NAMES
        assert not forbidden_names_found, (
            f"{module_path.relative_to(_CORE_DIR)} imports "
            f"{sorted(forbidden_names_found)}, which names a "
            "client/transaction-executor type. Modules below "
            "pytusk/client/ must never import or reference a client or "
            "transaction-executor -- doing so closes a real import cycle, "
            "not just a style violation."
        )

        forbidden_modules_found = {
            module
            for module in modules
            if any(substring in module for substring in _FORBIDDEN_MODULE_SUBSTRINGS)
        }
        assert not forbidden_modules_found, (
            f"{module_path.relative_to(_CORE_DIR)} imports from "
            f"{sorted(forbidden_modules_found)}, a client/transaction-executor "
            "module. Modules below pytusk/client/ must never import or "
            "reference a client or transaction-executor -- doing so closes "
            "a real import cycle, not just a style violation."
        )

    def test_at_least_expected_modules_are_checked(self) -> None:
        """Regression guard: the globs above must not silently match nothing.

        A typo in ``_CORE_DIR`` or the package directories would make
        every parametrized case above vacuously pass -- this pins that at
        least ``pytusk.core.types``, ``pytusk.core.encoding``,
        ``pytusk.core.chain``, and ``pytusk.core.certification`` were all
        found (one module per package plus ``certification.py`` at a
        minimum).
        """
        assert len(_below_client_modules()) >= 4
