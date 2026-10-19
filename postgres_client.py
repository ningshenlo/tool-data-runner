"""Explicit PostgreSQL statements; no SQL rewriting or automatic write retries."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class PostgresStatement:
    sql: str
    params: tuple[Any, ...] = ()
    label: str = ''

class PostgresClient:
    def __init__(self, database_url: str, *, max_connections: int = 1, connect_timeout: int = 10):
        if not database_url or not 1 <= max_connections <= 4:
            raise ValueError('PostgreSQL URL and a pool limit between 1 and 4 are required')
        from psycopg.rows import dict_row
        from psycopg.conninfo import conninfo_to_dict
        from psycopg_pool import AsyncConnectionPool
        options=conninfo_to_dict(database_url).get('options','')+' -c statement_timeout=30000 -c lock_timeout=2000 -c idle_in_transaction_session_timeout=30000'
        self.pool = AsyncConnectionPool(database_url, min_size=0, max_size=max_connections,
            open=False, timeout=connect_timeout,
            kwargs={'row_factory': dict_row, 'connect_timeout': connect_timeout,
                    'options': options.strip()})

    async def __aenter__(self):
        await self.pool.open()
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        await self.pool.close()

    @staticmethod
    def _validate(statement):
        if not isinstance(statement, PostgresStatement) or not statement.sql.strip() or not statement.label.strip():
            raise ValueError('An explicit labeled PostgreSQL statement is required')

    async def batch(self, statements: list[PostgresStatement]) -> list[dict[str, Any]]:
        for statement in statements: self._validate(statement)
        if not statements: return []
        # psycopg connection context commits on success and rolls back exceptions.
        # The pool may establish replacement connections; it never replays SQL.
        async with self.pool.connection() as connection:
            async with connection.transaction():
                results = []
                for statement in statements:
                    async with connection.cursor() as cursor:
                        await cursor.execute(statement.sql, statement.params)
                        rows = await cursor.fetchall() if cursor.description else []
                        changed = max(0, cursor.rowcount) if cursor.statusmessage.split(' ')[0] in {'INSERT','UPDATE','DELETE','MERGE'} else 0
                        results.append({'success': True, 'results': rows, 'meta': {'changes': changed}})
                return results

    async def execute(self, statement: PostgresStatement):
        return (await self.batch([statement]))[0]

    async def query(self, statement: PostgresStatement):
        return (await self.execute(statement))['results']

    async def run(self, statement: PostgresStatement):
        return (await self.execute(statement))['meta']
