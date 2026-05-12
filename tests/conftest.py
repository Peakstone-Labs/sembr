# SPDX-License-Identifier: Apache-2.0
"""Shared pytest fixtures.

Resets sembr.db.sqlite module-level globals (_conn, _WRITE_LOCK) before and after
every test. Required since the a3e5ef3 refactor introduced a process-global
_WRITE_LOCK that, if left pointing at a closed event loop from a previous test,
causes transaction() to hang indefinitely instead of failing fast.
"""

import pytest
from sembr.db import sqlite as _sqlite_mod


@pytest.fixture(autouse=True)
def reset_sqlite_state():
    _sqlite_mod._conn = None
    _sqlite_mod._WRITE_LOCK = None
    yield
    _sqlite_mod._conn = None
    _sqlite_mod._WRITE_LOCK = None
