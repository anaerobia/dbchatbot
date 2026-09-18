"""Database access layer.

The app talks to :class:`db.base.Database` instances only; the registry picks
the right one (Oracle, Postgres, ...) by name from the config. All execution
goes through :meth:`Database.run_select`, which refuses anything that isn't a
single read-only query (see :mod:`db.guard`).
"""

from db.base import Database
from db.guard import UnsafeQueryError, assert_read_only, is_read_only
from db.registry import (
    ConfigError,
    UnknownDatabaseError,
    get_database,
    get_registry,
    list_databases,
)

__all__ = [
    "ConfigError",
    "Database",
    "UnknownDatabaseError",
    "UnsafeQueryError",
    "assert_read_only",
    "get_database",
    "get_registry",
    "is_read_only",
    "list_databases",
]
