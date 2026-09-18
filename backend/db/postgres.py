"""PostgreSQL backend, using ``psycopg`` 3."""

from __future__ import annotations

from typing import Any

import psycopg

from db.base import Database

_POSTGRES_RULES = (
    "- PostgreSQL dialect. Do NOT end a read query with a semicolon.\n"
    "- To limit rows use `LIMIT n`.\n"
    "- Unquoted identifiers fold to lowercase; double-quote mixed-case names.\n"
    "- Qualify tables with their schema exactly as they appear in the schema "
    "listing (e.g. `sales.orders`)."
)

DEFAULT_SCHEMAS = ["public"]
DEFAULT_STATEMENT_TIMEOUT_MS = 60_000


class PostgresDatabase(Database):
    """A PostgreSQL database.

    Tables are introspected from ``information_schema.columns`` for the
    configured ``schemas`` (default ``["public"]``) and listed as
    ``schema.table``.
    """

    kind = "postgres"
    dialect = "PostgreSQL"
    dialect_rules = _POSTGRES_RULES

    def __init__(
        self,
        name: str,
        *,
        host: str,
        dbname: str,
        user: str,
        password: str,
        port: int = 5432,
        schemas: list[str] | None = None,
        sslmode: str | None = None,
        statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
        label: str | None = None,
    ) -> None:
        """Store connection settings; no connection is opened yet."""
        super().__init__(name, label)
        self._conninfo: dict[str, Any] = {
            "host": host,
            "port": int(port),
            "dbname": dbname,
            "user": user,
            "password": password,
            # Every session defaults to read-only transactions and is bounded
            # in run time, on top of the per-connection read_only flag below.
            "options": (
                "-c default_transaction_read_only=on "
                f"-c statement_timeout={int(statement_timeout_ms)}"
            ),
        }
        if sslmode:
            self._conninfo["sslmode"] = sslmode
        self._schemas = list(schemas or DEFAULT_SCHEMAS)

    def _connect(self) -> psycopg.Connection:
        """Open a new connection whose transactions are read-only."""
        conn = psycopg.connect(**self._conninfo)
        conn.read_only = True
        return conn

    def _execute_read_only(
        self, sql: str, max_rows: int
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Run ``sql`` in a read-only transaction (rolled back on close)."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            columns = [d.name for d in cur.description or []]
            rows = cur.fetchmany(max_rows) if cur.description else []
            conn.rollback()
            return columns, rows

    def _fetch_columns(self) -> list[tuple[str, str, str]]:
        """Read table/view column metadata from ``pg_catalog``.

        Uses ``pg_catalog`` rather than ``information_schema`` so that:

        * partitions (e.g. ``payment_p2022_01``) are skipped -- only the
          partitioned parent is listed, which is what queries should target;
        * enum/domain/array types show their real names (``mpaa_rating``,
          ``text[]``) instead of ``USER-DEFINED``/``ARRAY``;
        * materialized views are included;
        * only relations the user may ``SELECT`` from are listed;
        * names that need quoting (e.g. a ``"zip code"`` column) are quoted.
        """
        query = """
            SELECT quote_ident(n.nspname) || '.' || quote_ident(c.relname),
                   quote_ident(a.attname),
                   format_type(a.atttypid, a.atttypmod)
            FROM pg_catalog.pg_attribute a
            JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = ANY(%s)
              AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND NOT c.relispartition
              AND a.attnum > 0
              AND NOT a.attisdropped
              AND has_table_privilege(c.oid, 'SELECT')
            ORDER BY n.nspname, c.relname, a.attnum
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(query, (self._schemas,))
            return cur.fetchall()
