"""Compatibility shim — implementation lives in shared.db.client."""

from shared.db.client import *  # noqa: F403
from shared.db.client import PostgresClient, PostgresStatement

__all__ = ["PostgresClient", "PostgresStatement"]
