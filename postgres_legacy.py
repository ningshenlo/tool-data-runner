"""Compatibility shim — implementation lives in shared.db.legacy."""

from shared.db.legacy import *  # noqa: F403
from shared.db.legacy import (
    AINAV_DATABASE_ID,
    LegacyPostgresClient,
    catalog_entry,
    dynamic_postgres_entry,
    query_key,
    transport_for,
)

__all__ = [
    "AINAV_DATABASE_ID",
    "LegacyPostgresClient",
    "catalog_entry",
    "dynamic_postgres_entry",
    "query_key",
    "transport_for",
]
