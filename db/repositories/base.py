"""
Base repository class with common database operations.
"""

from typing import List, Optional, Any
import asyncpg


class BaseRepository:
    """
    Abstract base repository with common database operations.
    All repositories should inherit from this class.
    """

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def execute(self, query: str, *args) -> str:
        """Execute a query and return the status string."""
        async with self.pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def executemany(self, query: str, args: List[tuple]) -> None:
        """Execute a query multiple times with different arguments."""
        async with self.pool.acquire() as conn:
            await conn.executemany(query, args)

    async def fetch(self, query: str, *args) -> List[asyncpg.Record]:
        """Fetch multiple rows."""
        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args) -> Optional[asyncpg.Record]:
        """Fetch a single row."""
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args) -> Any:
        """Fetch a single value."""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(query, *args)
