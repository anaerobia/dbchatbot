"""Abstract database interface shared by every supported backend.

A :class:`Database` wraps one configured connection target (an Oracle
instance, a Postgres database, ...). The rest of the app only talks to this
interface; :mod:`db.registry` dispatches a request to the right instance by
name.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import Any

from db.guard import assert_read_only

NO_TABLES_MESSAGE = "(No tables found for this database user.)"


class Database(ABC):
    """One configured database the chatbot can query.

    Subclasses set :attr:`kind`, :attr:`dialect` and :attr:`dialect_rules`,
    and implement the driver-specific hooks :meth:`_execute_read_only`,
    :meth:`_fetch_columns` and :meth:`_fetch_server_version`.
    """

    #: Backend type identifier, as used in the config file (e.g. "oracle").
    kind: str = ""
    #: Human-readable SQL dialect name, used in LLM prompts (e.g. "Oracle").
    dialect: str = ""
    #: Dialect-specific rules for the SQL-generation prompt. Should cover both
    #: queries and data/DDL statements, since write requests are answered with
    #: SQL the user runs themselves.
    dialect_rules: str = ""
    #: Cheap query used by the health check.
    health_query: str = "SELECT 1"

    def __init__(self, name: str, label: str | None = None) -> None:
        """Initialize the shared state.

        Args:
            name: Unique identifier used by the API to select this database.
            label: Display name for the UI; defaults to ``name``.
        """
        self.name = name
        self.label = label or name
        self._schema_cache: str | None = None
        self._version_cache: str | None = None
        self._cache_lock = threading.Lock()

    def run_select(self, sql: str, max_rows: int) -> tuple[list[str], list[list]]:
        """Validate and execute a read-only query.

        Args:
            sql: The query to run. Must pass :func:`db.guard.assert_read_only`.
            max_rows: Hard cap on the number of rows fetched.

        Returns:
            A ``(columns, rows)`` tuple with JSON-friendly cell values.

        Raises:
            db.guard.UnsafeQueryError: If ``sql`` is not a safe read-only query.
        """
        safe_sql = assert_read_only(sql)
        columns, raw_rows = self._execute_read_only(safe_sql, max_rows)
        rows = [[_jsonable(v) for v in row] for row in raw_rows]
        return columns, rows

    def health_check(self) -> None:
        """Run :attr:`health_query`; raises if the database is unreachable."""
        self.run_select(self.health_query, max_rows=1)

    def get_schema_description(self) -> str:
        """Return a compact ``table(column type, ...)`` listing for the LLM.

        Computed once per process and cached (the schema is part of the
        prompt-cached prefix, so it should stay byte-stable).
        """
        with self._cache_lock:
            if self._schema_cache is None:
                self._schema_cache = _format_schema(self._fetch_columns())
            return self._schema_cache

    def get_server_version(self) -> str:
        """Return the database server version string (cached per process).

        Dialects differ by version (e.g. Oracle only has ``FETCH FIRST`` from
        12c and ``BOOLEAN`` from 23ai), so the version goes into the prompt.
        """
        with self._cache_lock:
            if self._version_cache is None:
                self._version_cache = self._fetch_server_version()
            return self._version_cache

    def dialect_context(self) -> str:
        """Return the dialect, server version and rules block for the LLM."""
        return (
            f"Database dialect: {self.dialect} "
            f"(server version {self.get_server_version()})\n"
            f"Write ALL SQL -- queries and change statements alike -- in this "
            f"dialect and version. Do not use syntax from other databases.\n"
            f"{self.dialect} rules:\n{self.dialect_rules}"
        )

    @abstractmethod
    def _execute_read_only(
        self, sql: str, max_rows: int
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Execute already-validated ``sql`` in a read-only session.

        Returns:
            Column names and at most ``max_rows`` raw driver rows.
        """

    @abstractmethod
    def _fetch_columns(self) -> list[tuple[str, str, str]]:
        """Return ``(table_name, column_name, data_type)`` rows, in order."""

    @abstractmethod
    def _fetch_server_version(self) -> str:
        """Return the server version as a human-readable string."""


def _format_schema(rows: list[tuple[str, str, str]]) -> str:
    """Group column rows into one ``table(col type, ...)`` line per table."""
    tables: dict[str, list[str]] = {}
    for table_name, column_name, data_type in rows:
        tables.setdefault(table_name, []).append(f"{column_name} {data_type}")

    if not tables:
        return NO_TABLES_MESSAGE
    return "\n".join(f"{table}({', '.join(cols)})" for table, cols in tables.items())


def _jsonable(value: Any) -> Any:
    """Coerce a driver cell value into something ``json.dumps`` can handle."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
