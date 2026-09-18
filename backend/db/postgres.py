"""PostgreSQL backend, using ``psycopg`` 3."""

from __future__ import annotations

from typing import Any

import psycopg

from db.base import Database

_POSTGRES_RULES = """\
- Do NOT end a read query with a semicolon.
- Row limits: `LIMIT n [OFFSET m]`. Never use ROWNUM, TOP, or `FROM dual`.
- Unquoted identifiers fold to lowercase; write names exactly as shown in the
  schema listing, including any double quotes (e.g. `"zip code"`).
- Qualify tables with their schema as in the listing (e.g. `public.film`).
- Dates: now() / CURRENT_DATE / CURRENT_TIMESTAMP; date_trunc('month', ts),
  EXTRACT(YEAR FROM ts), ts + INTERVAL '1 day', age(), to_char(ts, fmt),
  DATE '2024-01-31'. No SYSDATE, ADD_MONTHS, or TO_DATE-style Oracle idioms.
- Strings: `||` or concat(), substring(), position(), COALESCE (no NVL),
  string_agg(x, ',' ORDER BY x) (no LISTAGG). Case-insensitive match: ILIKE.
- Casts with `::type` or CAST. Integer division truncates -- cast to numeric
  for ratios. ROUND(x, n) only accepts numeric: cast double precision
  values (e.g. from avg() over floats, EXTRACT) to numeric first.
- Enum/domain columns (shown by their type name) compare to string literals.
  Arrays: `'x' = ANY(col)`, `col @> ARRAY['x']`. jsonb: `->`, `->>`, `@>`.
- Booleans are real BOOLEAN (`WHERE active`, `TRUE/FALSE`).
- FILTER (WHERE ...) on aggregates, DISTINCT ON, and window functions are
  available.
- DDL types: text / varchar(n), integer, bigint, numeric(p,s), boolean,
  timestamptz, date, jsonb. Auto keys: `GENERATED ALWAYS AS IDENTITY`.
- Upserts: INSERT ... ON CONFLICT (cols) DO UPDATE SET col = EXCLUDED.col
  (MERGE also exists in 15+). RETURNING is available. IF [NOT] EXISTS is
  supported on CREATE/DROP.
- Multi-row insert: INSERT ... VALUES (...), (...).
- Wrap multi-statement changes in BEGIN; ... COMMIT; so they apply atomically.
"""

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

    def _fetch_server_version(self) -> str:
        """Return the ``server_version`` reported by the server (e.g. ``18.0``)."""
        with self._connect() as conn:
            return conn.info.parameter_status("server_version") or "unknown"

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
