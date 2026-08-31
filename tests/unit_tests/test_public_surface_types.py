#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Enforces that every type an exported signature names is itself exported.

The project's stated public-surface rule ("everything public is reachable
from top-level ``pytusk``") is otherwise checked only by ``grep -rn 'from
pytusk.core' pytusk/tusky/'`` -- which catches the CLI reaching past the
surface, but NOT a different violation: an exported function or dataclass
whose own signature names a project-defined type that ``pytusk.__all__``
does not export. A caller using only the public API can hold such a value
(it round-trips through the exported function fine) but cannot import the
type to annotate or construct it.

Found live twice: Plan #27 (2026-08-31) shipped ``assemble_quilt`` and
``quilt_patch_id`` exported while their own return/parameter types
(``AssembledQuilt``, ``QuiltPatchLayout``) were not, and the same audit that
found that gap also found ``ExecuteOnlyClient`` referenced by four exported
native-upload functions without being exported itself. Both were fixed by
exporting the missing type, not by changing the referencing signature --
this test exists so the NEXT instance is caught automatically rather than
by a code review, per backlog #37.

Walks ``pytusk.__all__`` at runtime (not via ``ast``, unlike
``test_ops_layering.py``'s compose-module check) because the question here
is about RESOLVED type hints -- generics, ``X | None`` unions, and
inherited dataclass fields all need to be expanded to their leaf classes,
which only ``typing.get_type_hints`` (or a raw ``eval`` of the annotation)
can do; a syntax-level import scan cannot see through a type alias or a
generic parameter.

``_resolve_function_hints`` falls back to per-parameter ``eval`` when
``typing.get_type_hints`` raises -- observed on Python 3.10.6 for any
function annotated with a bare ``Callable[[X], Y] | None`` parameter
(``store_blob_relay``, ``store_quilt_relay``): CPython's typing machinery
fails to hash the combination of a callable generic alias with a union,
which is an interpreter quirk unrelated to whether the annotation is
correct. The fallback evaluates each parameter's annotation independently
against the defining module's globals, sidestepping whatever cross-hint
step in ``get_type_hints`` trips over the union.
"""

import dataclasses
import enum
import inspect
import logging
import sys
import typing

import pytusk

_LOGGER = logging.getLogger(__name__)

_EXPORTED = frozenset(pytusk.__all__)


def _leaf_types(tp: object, seen: set[int]) -> typing.Iterator[object]:
    """Yield every non-generic leaf type reachable from a type hint.

    Args:
        tp (object): A type hint, possibly a generic alias, union, or bare
            class.
        seen (set[int]): ``id()``s already yielded, so a self-referential or
            repeated type is not walked twice.

    Yields:
        object: Each leaf class found, exactly once.
    """
    if tp is None or tp is type(None):
        return
    origin = typing.get_origin(tp)
    if origin is not None:
        for arg in typing.get_args(tp):
            # Callable[[X, Y], Z] reports its parameter types as a LIST,
            # not a type -- unwrap it rather than treating the list itself
            # as a leaf.
            if isinstance(arg, list):
                for inner in arg:
                    yield from _leaf_types(inner, seen)
            else:
                yield from _leaf_types(arg, seen)
        return
    if isinstance(tp, typing.TypeVar):
        return
    if id(tp) in seen:
        return
    seen.add(id(tp))
    yield tp


def _is_project_type(tp: object) -> bool:
    """Report whether a type hint leaf is defined somewhere in ``pytusk``.

    Args:
        tp (object): A leaf type, as yielded by :func:`_leaf_types`.

    Returns:
        bool: True if the type's module is part of the ``pytusk`` package.
    """
    module = getattr(tp, "__module__", "")
    return isinstance(module, str) and module.startswith("pytusk")


def _resolve_function_hints(func: object) -> dict[str, object]:
    """Resolve a function's parameter and return annotations to real objects.

    Tries :func:`typing.get_type_hints` first; falls back to evaluating each
    parameter's annotation independently on failure. See the module
    docstring for why the fallback exists.

    Args:
        func (object): The function to resolve hints for.

    Returns:
        dict[str, object]: Parameter/return name to resolved type object.
    """
    try:
        return typing.get_type_hints(func)
    except (TypeError, NameError) as exc:
        _LOGGER.debug(
            "typing.get_type_hints failed for %s, falling back to "
            "per-parameter eval: %s",
            func,
            exc,
        )

    module_globals = vars(sys.modules[func.__module__])
    hints: dict[str, object] = {}
    signature = inspect.signature(func)
    for param_name, param in signature.parameters.items():
        raw = param.annotation
        if raw is inspect.Signature.empty:
            continue
        hints[param_name] = eval(raw, module_globals) if isinstance(raw, str) else raw
    if signature.return_annotation is not inspect.Signature.empty:
        raw = signature.return_annotation
        hints["return"] = eval(raw, module_globals) if isinstance(raw, str) else raw
    return hints


def _referenced_project_types_not_exported() -> list[tuple[str, str, str]]:
    """Find every exported name whose signature names an unexported type.

    Returns:
        list[tuple[str, str, str]]: ``(exported_name, missing_type_name,
        missing_type_module)`` for each gap found.
    """
    gaps: list[tuple[str, str, str]] = []
    for name in sorted(_EXPORTED):
        obj = getattr(pytusk, name, None)
        if obj is None:
            continue

        referenced: set[object] = set()
        if inspect.isclass(obj):
            if dataclasses.is_dataclass(obj):
                for field_type in typing.get_type_hints(obj).values():
                    referenced.update(_leaf_types(field_type, set()))
            elif issubclass(obj, (enum.Enum, BaseException)):
                continue
            else:
                try:
                    init_hints = typing.get_type_hints(obj.__init__)
                except (TypeError, NameError) as exc:
                    _LOGGER.debug(
                        "typing.get_type_hints failed for %s.__init__, "
                        "skipping its __init__ signature: %s",
                        name,
                        exc,
                    )
                    continue
                for param_type in init_hints.values():
                    referenced.update(_leaf_types(param_type, set()))
        elif inspect.isfunction(obj):
            for param_type in _resolve_function_hints(obj).values():
                referenced.update(_leaf_types(param_type, set()))
        else:
            continue

        for leaf in referenced:
            if not _is_project_type(leaf):
                continue
            leaf_name = getattr(leaf, "__name__", str(leaf))
            if leaf_name not in _EXPORTED:
                gaps.append((name, leaf_name, leaf.__module__))
    return gaps


class TestExportedSignaturesReferenceOnlyExportedTypes:
    """Every project-defined type an exported signature names is exported."""

    def test_no_gaps(self) -> None:
        """No exported function/dataclass names an unexported project type."""
        gaps = _referenced_project_types_not_exported()
        assert not gaps, (
            "Exported pytusk names reference project-defined types that "
            "are themselves not exported -- a caller using only the public "
            "API cannot import the type its own signature names:\n"
            + "\n".join(
                f"  pytusk.{owner} references {missing!r} from {module}, "
                "which is not in pytusk.__all__"
                for owner, missing, module in gaps
            )
        )

    def test_at_least_expected_names_are_audited(self) -> None:
        """Guards against an empty/broken __all__ making test_no_gaps vacuous."""
        assert len(_EXPORTED) >= 100, (
            f"pytusk.__all__ has only {len(_EXPORTED)} names -- far fewer "
            "than expected. test_no_gaps would pass vacuously over a "
            "broken export list rather than actually checking anything."
        )
