from __future__ import annotations

from typing import TypeAlias

from .postgres import PostgresRepository
from .security import TokenCipher
from .sqlite_repository import SQLiteRepository

PersistentRepository: TypeAlias = PostgresRepository | SQLiteRepository


def create_repository(database_url: str, token_cipher: TokenCipher) -> PersistentRepository:
    if database_url.startswith("sqlite:///"):
        return SQLiteRepository(database_url, token_cipher)
    if database_url.startswith(("postgresql://", "postgres://")):
        return PostgresRepository(database_url, token_cipher)
    raise ValueError("DATABASE_URL must use sqlite:/// or postgresql://")
