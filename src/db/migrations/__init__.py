"""Ordered, immutable PostgreSQL schema migrations."""

from .v0001_initial_schema import MIGRATION as V0001_INITIAL_SCHEMA

MIGRATIONS = (V0001_INITIAL_SCHEMA,)
