"""Oracle backend, using ``oracledb`` in thin mode (no client install needed)."""

from __future__ import annotations

from typing import Any

import oracledb

from db.base import Database

_ORACLE_RULES = (
    "- Oracle SQL dialect. Do NOT end a read query with a semicolon.\n"
    "- To limit rows use `FETCH FIRST n ROWS ONLY`.\n"
    "- Unquoted identifiers are case-insensitive and reported in UPPERCASE."
)


class OracleDatabase(Database):
    """An Oracle instance addressed by SID or service name.

    If ``schemas`` is given, tables are introspected from ``ALL_TAB_COLUMNS``
    for those owners (qualified as ``OWNER.TABLE``); otherwise from
    ``USER_TAB_COLUMNS`` (the connecting user's own tables).
    """

    kind = "oracle"
    dialect = "Oracle"
    dialect_rules = _ORACLE_RULES
    health_query = "SELECT 1 FROM dual"

    def __init__(
        self,
        name: str,
        *,
        host: str,
        user: str,
        password: str,
        port: int = 1521,
        sid: str | None = None,
        service_name: str | None = None,
        schemas: list[str] | None = None,
        label: str | None = None,
    ) -> None:
        """Store connection settings; no connection is opened yet.

        Raises:
            ValueError: If neither or both of ``sid``/``service_name`` are set.
        """
        super().__init__(name, label)
        if bool(sid) == bool(service_name):
            raise ValueError(
                f"Oracle database '{name}': set exactly one of 'sid' or "
                "'service_name'."
            )
        self._user = user
        self._password = password
        self._dsn = oracledb.makedsn(
            host, int(port), sid=sid, service_name=service_name
        )
        self._schemas = [s.upper() for s in schemas or []]

    def _connect(self) -> oracledb.Connection:
        """Open a new connection."""
        return oracledb.connect(user=self._user, password=self._password, dsn=self._dsn)

    def _execute_read_only(
        self, sql: str, max_rows: int
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Run ``sql`` inside a ``READ ONLY`` transaction."""
        with self._connect() as conn:
            conn.autocommit = False
            try:
                with conn.cursor() as setup:
                    setup.execute("SET TRANSACTION READ ONLY")
            except oracledb.DatabaseError:
                # Some configurations disallow this; the string guard and the
                # read-only DB user still apply.
                pass
            with conn.cursor() as cur:
                cur.execute(sql)
                columns = [d[0] for d in cur.description]
                return columns, cur.fetchmany(max_rows)

    def _fetch_columns(self) -> list[tuple[str, str, str]]:
        """Read table/column metadata from the Oracle data dictionary."""
        if self._schemas:
            binds = {f"s{i}": s for i, s in enumerate(self._schemas)}
            placeholders = ", ".join(f":{k}" for k in binds)
            query = f"""
                SELECT owner || '.' || table_name, column_name, data_type
                FROM all_tab_columns
                WHERE owner IN ({placeholders})
                ORDER BY owner, table_name, column_id
            """
        else:
            binds = {}
            query = """
                SELECT table_name, column_name, data_type
                FROM user_tab_columns
                ORDER BY table_name, column_id
            """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(query, binds)
            return cur.fetchall()
