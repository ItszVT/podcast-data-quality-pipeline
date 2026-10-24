"""Resolve the configured database backend without opening a connection."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Mapping
from urllib.parse import urlsplit

DatabaseBackend = Literal["sqlite", "postgres"]


class DatabaseConfigurationError(ValueError):
    """Raised when database backend settings are missing or unsupported."""


@dataclass(frozen=True)
class DatabaseConfig:
    """Database selection; the connection URL is excluded from repr output."""

    backend: DatabaseBackend
    sqlite_path: Path | None = None
    database_url: str | None = field(default=None, repr=False)


def load_database_config(
    sqlite_path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> DatabaseConfig:
    """Read backend settings without connecting to or modifying a database.

    SQLite remains the default. PostgreSQL must be selected explicitly and
    requires a PostgreSQL URL. URL values are never included in errors.
    """
    settings = os.environ if environ is None else environ
    backend = settings.get("PODCAST_DB_BACKEND", "sqlite").strip().lower()

    if backend not in {"sqlite", "postgres"}:
        raise DatabaseConfigurationError(
            "Unsupported PODCAST_DB_BACKEND value. Use 'sqlite' or 'postgres'."
        )

    if backend == "postgres":
        database_url = settings.get("PODCAST_DATABASE_URL", "").strip()
        if not database_url:
            raise DatabaseConfigurationError(
                "PODCAST_DATABASE_URL is required when PODCAST_DB_BACKEND is 'postgres'."
            )
        try:
            parsed_url = urlsplit(database_url)
            hostname = parsed_url.hostname
            # Accessing .port validates malformed and out-of-range port syntax.
            parsed_url.port
        except ValueError as exc:
            raise DatabaseConfigurationError(
                "PODCAST_DATABASE_URL must be a well-formed PostgreSQL URL with a hostname."
            ) from exc

        if parsed_url.scheme.lower() not in {"postgresql", "postgres"}:
            raise DatabaseConfigurationError(
                "PODCAST_DATABASE_URL must use the 'postgresql' or 'postgres' URL scheme."
            )
        if not parsed_url.netloc or not hostname or any(char.isspace() for char in hostname):
            raise DatabaseConfigurationError(
                "PODCAST_DATABASE_URL must be a well-formed PostgreSQL URL with a hostname."
            )
        return DatabaseConfig(backend="postgres", database_url=database_url)

    return DatabaseConfig(
        backend="sqlite",
        sqlite_path=Path(sqlite_path).expanduser() if sqlite_path is not None else None,
    )


def require_sqlite_backend(sqlite_path: str | Path | None = None) -> DatabaseConfig:
    """Fail closed at SQLite-only entry points if PostgreSQL was selected."""
    config = load_database_config(sqlite_path)
    if config.backend == "postgres":
        raise DatabaseConfigurationError(
            "PostgreSQL is selected, but PostgreSQL persistence and dashboard access "
            "are not implemented yet. No SQLite fallback was used."
        )
    return config
