"""SQLite 连接管理：单库连接 + 写锁，保证 FastAPI 多线程下串行写入、可重启恢复。

SQLite 在 WAL 下支持并发读，但同一时刻只允许一个写者；用一把可重入锁把所有
写事务串行化，既避免 "database is locked"，也让"恢复只成功一次"这类
检查-再-写入的原子语义在进程内成立（跨进程由 UNIQUE 约束兜底）。
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Any

from app.archive import db as db_schema
from app.config import settings


class ArchiveEngine:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or settings.archive_db_path
        self._conn: sqlite3.Connection | None = None
        self._write_lock = threading.RLock()

    def _ensure(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = db_schema.connect(self.db_path)
            db_schema.initialize(self._conn)
        return self._conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self._ensure()

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        row = self.conn.execute(sql, params).fetchone()
        return db_schema.row_to_dict(row)

    def query_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self._write_lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return int(cur.lastrowid)

    def executemany(self, sql: str, seq: list[tuple[Any, ...]]) -> None:
        with self._write_lock:
            self.conn.executemany(sql, seq)
            self.conn.commit()

    @property
    def write_lock(self) -> threading.RLock:
        return self._write_lock

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


# 进程级单例；测试可用 init_engine(临时路径) 替换。
_engine: ArchiveEngine | None = None
_engine_lock = threading.Lock()


def get_engine() -> ArchiveEngine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = ArchiveEngine()
    return _engine


def init_engine(db_path: str) -> ArchiveEngine:
    """测试/部署初始化：指向指定 SQLite 文件并立即建表。"""
    global _engine
    with _engine_lock:
        if _engine is not None:
            _engine.close()
        _engine = ArchiveEngine(db_path)
        _engine.conn  # 触发建表
    return _engine


def reset_engine() -> None:
    global _engine
    with _engine_lock:
        if _engine is not None:
            _engine.close()
        _engine = None
