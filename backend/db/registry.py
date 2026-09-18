"""Load the configured databases and dispatch to them by name.

Configuration comes from a JSON file (``databases.json`` next to ``main.py``,
or the path in ``DATABASES_CONFIG``)::

    {
      "default": "oracle_main",
      "databases": [
        {"name": "oracle_main", "type": "oracle", "host": "...", "sid": "...",
         "user": "...", "password_env": "ORACLE_MAIN_PASSWORD"},
        {"name": "analytics", "type": "postgres", "host": "...",
         "dbname": "...", "user": "...", "password_env": "ANALYTICS_PASSWORD"}
      ]
    }

Each entry needs ``name`` and ``type``; every other key is passed to the
backend's constructor. Secrets can be given inline as ``password`` or, better,
read from an environment variable named by ``password_env``.

If no config file exists, a single Oracle database named ``oracle`` is built
from the legacy ``ORACLE_*`` environment variables, so existing ``.env`` files
keep working.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from db.base import Database
from db.oracle import OracleDatabase
from db.postgres import PostgresDatabase

BACKENDS: dict[str, type[Database]] = {
    "oracle": OracleDatabase,
    "postgres": PostgresDatabase,
    "postgresql": PostgresDatabase,
}

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "databases.json"
LEGACY_ORACLE_NAME = "oracle"


class ConfigError(ValueError):
    """Raised when the database configuration is invalid."""


class UnknownDatabaseError(KeyError):
    """Raised when a request names a database that is not configured."""


class Registry:
    """The configured databases, keyed by name, plus the default choice."""

    def __init__(self, databases: list[Database], default: str | None) -> None:
        """Index ``databases`` by name.

        Raises:
            ConfigError: If names are duplicated, the list is empty, or
                ``default`` is not one of them.
        """
        if not databases:
            raise ConfigError("No databases configured.")
        self._by_name: dict[str, Database] = {}
        for database in databases:
            if database.name in self._by_name:
                raise ConfigError(f"Duplicate database name '{database.name}'.")
            self._by_name[database.name] = database
        self.default = default or databases[0].name
        if self.default not in self._by_name:
            raise ConfigError(f"Default database '{self.default}' is not configured.")

    def get(self, name: str | None = None) -> Database:
        """Return the database called ``name`` (the default if ``name`` is empty).

        Raises:
            UnknownDatabaseError: If no database has that name.
        """
        key = name or self.default
        try:
            return self._by_name[key]
        except KeyError:
            raise UnknownDatabaseError(key) from None

    def all(self) -> list[Database]:
        """Return every configured database, in config order."""
        return list(self._by_name.values())


def _build(entry: dict[str, Any]) -> Database:
    """Instantiate one backend from a config entry."""
    params = dict(entry)
    name = params.pop("name", None)
    kind = str(params.pop("type", "")).lower()
    if not name:
        raise ConfigError(f"Database entry is missing 'name': {entry!r}")
    backend = BACKENDS.get(kind)
    if backend is None:
        raise ConfigError(
            f"Database '{name}' has unsupported type '{kind}'. "
            f"Supported: {', '.join(sorted(BACKENDS))}."
        )

    password_env = params.pop("password_env", None)
    if password_env:
        if password_env not in os.environ:
            raise ConfigError(
                f"Database '{name}': environment variable '{password_env}' "
                "is not set."
            )
        params["password"] = os.environ[password_env]

    try:
        return backend(name, **params)
    except TypeError as exc:
        raise ConfigError(f"Database '{name}': invalid settings ({exc}).") from exc


def _from_file(path: Path) -> Registry:
    """Build a registry from a JSON config file."""
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc
    entries = config.get("databases", [])
    return Registry([_build(e) for e in entries], config.get("default"))


def _from_legacy_env() -> Registry:
    """Build a single-Oracle registry from the ``ORACLE_*`` variables."""
    required = ["ORACLE_HOST", "ORACLE_SID", "ORACLE_USER", "ORACLE_PASSWORD"]
    missing = [var for var in required if var not in os.environ]
    if missing:
        raise ConfigError(
            "No databases.json found and legacy Oracle settings are incomplete "
            f"(missing: {', '.join(missing)})."
        )
    oracle = OracleDatabase(
        LEGACY_ORACLE_NAME,
        label="Oracle",
        host=os.environ["ORACLE_HOST"],
        port=int(os.environ.get("ORACLE_PORT", "1521")),
        sid=os.environ["ORACLE_SID"],
        user=os.environ["ORACLE_USER"],
        password=os.environ["ORACLE_PASSWORD"],
    )
    return Registry([oracle], LEGACY_ORACLE_NAME)


def load_registry() -> Registry:
    """Load the registry from the config file, or fall back to legacy env vars."""
    path = Path(os.environ.get("DATABASES_CONFIG", DEFAULT_CONFIG_PATH))
    if path.exists():
        return _from_file(path)
    return _from_legacy_env()


_registry: Registry | None = None
_registry_lock = threading.Lock()


def get_registry() -> Registry:
    """Return the process-wide registry, loading it on first use."""
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = load_registry()
        return _registry


def get_database(name: str | None = None) -> Database:
    """Return the configured database called ``name`` (or the default)."""
    return get_registry().get(name)


def list_databases() -> list[Database]:
    """Return every configured database."""
    return get_registry().all()
