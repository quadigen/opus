from __future__ import annotations

from typing import Any, Callable, TypeVar

from synapse.module_api import ModuleApi
from synapse.storage.engines import PostgresEngine, Sqlite3Engine

from .util import json_dumps, json_loads

T = TypeVar("T")


class _SQLiteTxn:
    def __init__(self, txn: Any) -> None:
        self._txn = txn

    def execute(self, sql: str, args: Any = ()) -> Any:
        return self._txn.execute(sql.replace("%s", "?"), args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._txn, name)


class Database:
    def __init__(self, api: ModuleApi) -> None:
        self.api = api
        engine = api._store.db_pool.engine
        self.is_postgres = isinstance(engine, PostgresEngine)
        self.is_sqlite = isinstance(engine, Sqlite3Engine)
        if not self.is_postgres and not self.is_sqlite:
            raise RuntimeError("Kinemium supports PostgreSQL and SQLite only")

    async def run(
        self,
        description: str,
        func: Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        if self.is_sqlite:
            def sqlite_func(txn: Any, *sqlite_args: Any, **sqlite_kwargs: Any) -> T:
                return func(
                    _SQLiteTxn(txn), *sqlite_args, **sqlite_kwargs
                )

            return await self.api.run_db_interaction(
                description, sqlite_func, *args, **kwargs
            )
        return await self.api.run_db_interaction(
            description, func, *args, **kwargs
        )

    def for_update(self) -> str:
        return " FOR UPDATE" if self.is_postgres else ""

    def json(self, value: Any) -> str:
        return json_dumps(value)

    def load_json(self, value: Any) -> Any:
        return json_loads(value)
