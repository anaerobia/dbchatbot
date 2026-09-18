"""Dialect-independent SQL safety checks.

The chatbot only ever *executes* single read-only ``SELECT``/``WITH``
statements. Anything else (DML, DDL, statement chaining, PL/SQL blocks, ...)
is classified as a write: the app shows it to the user to copy and run
themselves, but never executes it.

This is defense in depth. Each backend also opens its session/transaction as
read-only, and the app should connect as a read-only database user (see
README).
"""

from __future__ import annotations

import re

# A read-only query must start with one of these.
_READ_LEADING = frozenset({"select", "with"})

# Keywords that can hide a write *inside* a SELECT/WITH statement. Matched as
# whole words anywhere (after string literals, quoted identifiers and comments
# are removed), catching Postgres data-modifying CTEs
# (``WITH d AS (DELETE ... RETURNING *) SELECT ...``), ``SELECT ... INTO``
# (creates a table in Postgres) and ``SELECT ... FOR UPDATE`` row locks. Kept
# deliberately small so ordinary column names (``comment``, ``set``, ...) don't
# trip it; DDL and other verbs are already excluded by the leading-word check.
_EMBEDDED_WRITE_KEYWORDS = frozenset({"insert", "update", "delete", "merge", "into"})

_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
_QUOTED_IDENTIFIER = re.compile(r'"(?:[^"]|"")*"')
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_WORD = re.compile(r"[a-z_]+")


class UnsafeQueryError(ValueError):
    """Raised when a statement is not a safe, single read-only query."""


def _strip_trailing_semicolon(sql: str) -> str:
    """Return ``sql`` trimmed, without one trailing semicolon."""
    cleaned = sql.strip()
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].strip()
    return cleaned


def _code_only(sql: str) -> str:
    """Return ``sql`` lowercased with literals, quoted names and comments removed.

    Quoted identifiers are removed so a column literally named ``"update"``
    doesn't look like a write keyword.
    """
    code = _BLOCK_COMMENT.sub(" ", sql)
    code = _LINE_COMMENT.sub(" ", code)
    code = _STRING_LITERAL.sub("''", code)
    code = _QUOTED_IDENTIFIER.sub('""', code)
    return code.lower()


def assert_read_only(sql: str) -> str:
    """Validate that ``sql`` is a single read-only query.

    Args:
        sql: The statement to check.

    Returns:
        The cleaned SQL, with a trailing semicolon stripped (drivers such as
        ``oracledb`` reject it).

    Raises:
        UnsafeQueryError: If the statement is empty, chained, does not start
            with ``SELECT``/``WITH``, or contains a write keyword.
    """
    cleaned = _strip_trailing_semicolon(sql)
    if not cleaned:
        raise UnsafeQueryError("Empty query.")

    code = _code_only(cleaned)
    if ";" in code:
        raise UnsafeQueryError("Multiple statements are not allowed.")

    words = _WORD.findall(code)
    if not words or words[0] not in _READ_LEADING:
        raise UnsafeQueryError("Only SELECT/WITH queries are allowed.")

    forbidden = sorted(set(words) & _EMBEDDED_WRITE_KEYWORDS)
    if forbidden:
        raise UnsafeQueryError(
            f"Statement contains non-read-only keyword(s): {', '.join(forbidden)}."
        )
    return cleaned


def is_read_only(sql: str) -> bool:
    """Return True if ``sql`` passes :func:`assert_read_only`."""
    try:
        assert_read_only(sql)
    except UnsafeQueryError:
        return False
    return True
