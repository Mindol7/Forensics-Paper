"""Persistent storage facade (SQLite now; MySQL migration boundary)."""

from Storage.db import DatabaseManager, sqlite_database_path

__all__ = ["DatabaseManager", "sqlite_database_path"]
