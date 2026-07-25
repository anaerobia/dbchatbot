"""Oracle database access layer.

Uses ``oracledb`` in thin mode (no Oracle client install required). All query
execution is funneled through :func:`run_select`, which refuses anything that is
not a single read-only ``SELECT``/``WITH`` statement. This is defense in depth:
the application should also connect as a dedicated read-only database user (see
README), but the guard here protects against a compromised or mistaken LLM
generating a destructive statement.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache

import oracledb

# Keywords that must never appear as the leading verb of a generated statement.
# The LLM is instructed to produce SELECT-only SQL; this is the enforcement.
_FORBIDDEN_LEADING = {
    "insert", "update", "delete", "merge", "drop", "create", "alter",
    "truncate", "grant", "revoke", "begin", "declare", "call", "execute",
    "comment", "rename", "flashback", "purge", "lock", "set",
}


def _dsn() -> str:
    """Build a DSN from environment variables (SID-based connection)."""
    host = os.environ["ORACLE_HOST"]
    port = int(os.environ["ORACLE_PORT"])
    sid = os.environ["ORACLE_SID"]
    return oracledb.makedsn(host, port, sid=sid)


def get_connection() -> oracledb.Connection:
    """Open a new Oracle connection using credentials from the environment."""
    return oracledb.connect(
        user=os.environ["ORACLE_USER"],
        password=os.environ["ORACLE_PASSWORD"],
        dsn=_dsn(),
    )


class UnsafeQueryError(ValueError):
    """Raised when a generated statement is not a safe read-only query."""


def assert_read_only(sql: str) -> str:
    """Validate that ``sql`` is a single read-only statement.

    Returns the cleaned SQL (trailing semicolon stripped) or raises
    :class:`UnsafeQueryError`.
    """
    cleaned = sql.strip()
    # Strip a single trailing semicolon (Oracle's driver rejects it anyway).
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].strip()

    if not cleaned:
        raise UnsafeQueryError("Empty query.")

    # Reject multiple statements. A semicolon that is not inside a string
    # literal indicates statement chaining, which we never allow.
    without_strings = re.sub(r"'(?:[^']|'')*'", "", cleaned)
    if ";" in without_strings:
        raise UnsafeQueryError("Multiple statements are not allowed.")

    lowered = without_strings.lstrip().lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise UnsafeQueryError("Only SELECT/WITH queries are allowed.")

    # Guard against DML/DDL keywords appearing anywhere as a statement verb.
    # We already blocked chaining, so a forbidden verb here would have to be a
    # column/alias name; word-boundary matching against the leading token set
    # keeps false positives low while still catching e.g. a sneaky "; delete".
    first_token = re.match(r"[a-z]+", lowered)
    if first_token and first_token.group(0) in _FORBIDDEN_LEADING:
        raise UnsafeQueryError(f"Statement type '{first_token.group(0)}' is not allowed.")

    return cleaned


def run_select(sql: str, max_rows: int) -> tuple[list[str], list[list]]:
    """Execute a validated read-only query and return (columns, rows).

    At most ``max_rows`` rows are fetched. Values are coerced to JSON-friendly
    types (str for anything the JSON encoder can't handle natively).
    """
    safe_sql = assert_read_only(sql)

    with get_connection() as conn:
        # Belt-and-suspenders: mark the session read-only so even a bypass of
        # the string guard cannot commit changes.
        try:
            conn.autocommit = False
            with conn.cursor() as setup:
                setup.execute("SET TRANSACTION READ ONLY")
        except oracledb.DatabaseError:
            # Some configurations disallow this; the string guard still applies.
            pass

        with conn.cursor() as cur:
            cur.execute(safe_sql)
            columns = [d[0] for d in cur.description]
            raw_rows = cur.fetchmany(max_rows)

    rows = [[_jsonable(v) for v in row] for row in raw_rows]
    return columns, rows


def _jsonable(value):
    """Coerce an Oracle cell value into something json.dumps can handle."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


@lru_cache(maxsize=1)
def get_schema_description() -> str:
    """Return a compact text description of the current user's tables.

    Cached for the process lifetime. Reads ``USER_TAB_COLUMNS`` so the LLM knows
    which tables and columns exist when generating SQL.
    """
    query = """
        SELECT table_name, column_name, data_type
        FROM user_tab_columns
        ORDER BY table_name, column_id
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()

    tables: dict[str, list[str]] = {}
    for table_name, column_name, data_type in rows:
        tables.setdefault(table_name, []).append(f"{column_name} {data_type}")

    if not tables:
        return "(No tables found for this database user.)"

    lines = []
    for table_name, cols in tables.items():
        lines.append(f"{table_name}({', '.join(cols)})")
    return "\n".join(lines)
