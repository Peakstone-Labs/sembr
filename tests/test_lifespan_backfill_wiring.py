# SPDX-License-Identifier: Apache-2.0
"""Assembly guard for the derived-backfill wiring inside ``lifespan``.

The backfill's pending flag has exactly one owner: the ``initialise_pending_flag``
call in ``sembr/main.py``. Everything downstream (the search endpoint's
under-recall warning) is well tested against a flag that already exists — so
deleting that single call, or letting the job registration run first, passes
every other test in the suite while reopening the silent gap between process
start and the job's first round.

Driving the real lifespan would need Qdrant, SQLite and a loaded embedder, so
this reads the module's own AST instead. It is a structural assertion, not a
behavioural one, and it is deliberately narrow: it fails when the call is
removed, moved out of ``lifespan``, reordered after the job registration, or
when the job's returned state stops being kept.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import sembr.main


def _lifespan_ast() -> ast.AsyncFunctionDef:
    tree = ast.parse(Path(sembr.main.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan":
            return node
    raise AssertionError("sembr/main.py no longer defines `async def lifespan`")


def _awaited_names(fn: ast.AST) -> set[str]:
    """Names called directly under an ``await``.

    Dropping the ``await`` leaves the call site looking correct while the
    coroutine never runs — the same end state as deleting the line, but it
    surfaces only as a RuntimeWarning buried in the startup log.
    """
    names: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name:
            names.add(name)
    return names


def _called_names_in_order(fn: ast.AST) -> list[str]:
    calls: list[tuple[int, str]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name:
            calls.append((node.lineno, name))
    return [name for _lineno, name in sorted(calls)]


def test_lifespan_publishes_pending_flag_before_registering_the_job():
    names = _called_names_in_order(_lifespan_ast())

    assert "initialise_pending_flag" in names, (
        "lifespan no longer initialises the backfill pending flag; every restart "
        "would then serve derived-field queries with no under-recall warning "
        "until the job's first round"
    )
    assert "add_news_derived_backfill_job" in names
    assert names.index("initialise_pending_flag") < names.index("add_news_derived_backfill_job"), (
        "the flag must have a value before the job that maintains it is registered"
    )


def test_lifespan_awaits_the_pending_flag_initialisation():
    assert "initialise_pending_flag" in _awaited_names(_lifespan_ast()), (
        "initialise_pending_flag is a coroutine; without `await` it never runs "
        "and the flag is never published"
    )


def test_lifespan_keeps_the_backfill_state_handle():
    """The quarantine set lives only in that state object; dropping the return
    value leaves the operator endpoint with nothing to report."""
    for node in ast.walk(_lifespan_ast()):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if not isinstance(value, ast.Call):
            continue
        func = value.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "add_news_derived_backfill_job":
            continue
        targets = [t.attr for t in node.targets if isinstance(t, ast.Attribute)]
        assert "news_derived_backfill_state" in targets
        return
    pytest.fail(
        "add_news_derived_backfill_job()'s return value is discarded; "
        "app.state.news_derived_backfill_state is the only handle on the "
        "quarantine set"
    )
