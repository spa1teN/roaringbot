"""
Database connection management using asyncpg.
Provides a singleton DatabaseManager with connection pooling.
Mirrors the pattern used by the sibling Tausendsassa bot (db/connection.py).
"""

import asyncio
import logging
from typing import Optional, TYPE_CHECKING

import asyncpg

if TYPE_CHECKING:
    from db.repositories.moderation_repository import ModerationRepository
    from db.repositories.guild_repository import GuildRepository
    from db.repositories.esports_repository import EsportsRepository
    from db.repositories.birthday_repository import BirthdayRepository

log = logging.getLogger("roaringbot.db")


class DatabaseManager:
    """
    Manages PostgreSQL connection pool and provides access to repositories.

    Usage:
        db = await get_db()
        config = await db.moderation.get_guild_config(guild_id)
    """

    _instance: Optional['DatabaseManager'] = None
    _pool: Optional[asyncpg.Pool] = None
    _lock = asyncio.Lock()

    def __init__(self):
        self._repositories = {}
        self._connected = False

    @classmethod
    async def get_instance(cls) -> 'DatabaseManager':
        """Get or create the singleton DatabaseManager instance."""
        async with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            if not cls._instance._connected:
                await cls._instance.connect()
            return cls._instance

    async def connect(
        self,
        host: str = None,
        port: int = None,
        database: str = None,
        user: str = None,
        password: str = None,
    ) -> None:
        """Initialize the connection pool."""
        import os

        host = host or os.getenv('DB_HOST', 'localhost')
        port = port or int(os.getenv('DB_PORT', 5432))
        database = database or os.getenv('DB_NAME', 'roaringbot')
        user = user or os.getenv('DB_USER', 'roaringbot')
        password = password or os.getenv('DB_PASSWORD', '')

        try:
            self._pool = await asyncpg.create_pool(
                host=host,
                port=port,
                database=database,
                user=user,
                password=password,
                min_size=2,
                max_size=10,
                command_timeout=60,
                statement_cache_size=100,
            )
            self._connected = True
            log.info(f"Database connection pool established: {host}:{port}/{database}")
        except Exception as e:
            log.error(f"Failed to connect to database: {e}")
            raise

    async def close(self) -> None:
        """Close the connection pool."""
        if self._pool:
            await self._pool.close()
            self._pool = None
            self._connected = False
            self._repositories.clear()
            log.info("Database connection pool closed")

    @property
    def pool(self) -> asyncpg.Pool:
        """Get the connection pool, raising if not connected."""
        if not self._pool:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._pool

    @property
    def is_connected(self) -> bool:
        """Check if database is connected."""
        return self._connected and self._pool is not None

    # Repository accessors with lazy initialization

    @property
    def guilds(self) -> 'GuildRepository':
        if 'guilds' not in self._repositories:
            from db.repositories.guild_repository import GuildRepository
            self._repositories['guilds'] = GuildRepository(self.pool)
        return self._repositories['guilds']

    @property
    def moderation(self) -> 'ModerationRepository':
        if 'moderation' not in self._repositories:
            from db.repositories.moderation_repository import ModerationRepository
            self._repositories['moderation'] = ModerationRepository(self.pool)
        return self._repositories['moderation']

    @property
    def esports(self) -> 'EsportsRepository':
        if 'esports' not in self._repositories:
            from db.repositories.esports_repository import EsportsRepository
            self._repositories['esports'] = EsportsRepository(self.pool)
        return self._repositories['esports']

    @property
    def birthdays(self) -> 'BirthdayRepository':
        if 'birthdays' not in self._repositories:
            from db.repositories.birthday_repository import BirthdayRepository
            self._repositories['birthdays'] = BirthdayRepository(self.pool)
        return self._repositories['birthdays']


# Global accessor function
async def get_db() -> DatabaseManager:
    """Get the database manager instance."""
    return await DatabaseManager.get_instance()
