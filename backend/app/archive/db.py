"""归档迁移库（SQLite）：把实验归档库的进程内状态固化成可恢复、可审计的持久数据。

设计要点：
- 一个迁移包按"原归档号"整包迁移；同一实验编码的不同归档版次各占一行，
  UNIQUE(archive_no, version) 保证不会因为实验编码相同而合并版次。
- 迁移按阶段推进（package_journal），每个阶段都有落库记录，重启后可继续。
- 恢复请求、切片恢复结果、部署事件全部持久化，重启或回滚后仍可说明处理结果。
- 所有写操作走短事务；WAL 保证部署切换/回滚时数据不丢。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.config import settings

SCHEMA_VERSION = 1

DDL = [
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # 迁移包：原归档号 + 归档版次 唯一（不按实验编码合并版次）
    """
    CREATE TABLE IF NOT EXISTS migration_packages (
        archive_no        TEXT NOT NULL,
        version           TEXT NOT NULL,
        exp_code          TEXT NOT NULL,
        storage_version   TEXT NOT NULL,
        stage             TEXT NOT NULL,
        package_status    TEXT NOT NULL,
        summary           TEXT,
        metrics_json      TEXT,
        evidence_json     TEXT,
        source_payload_json TEXT,
        payload_hash      TEXT,
        missing_fields_json TEXT,
        total_stages      INTEGER NOT NULL DEFAULT 0,
        finished_stages   INTEGER NOT NULL DEFAULT 0,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL,
        PRIMARY KEY (archive_no, version)
    )
    """,
    # 迁移阶段流水：摘要 / 指标 / 恢复依据 三段整包迁移
    """
    CREATE TABLE IF NOT EXISTS package_journal (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        archive_no   TEXT NOT NULL,
        version      TEXT NOT NULL,
        stage        TEXT NOT NULL,
        stage_status TEXT NOT NULL,
        detail       TEXT,
        recorded_at  TEXT NOT NULL,
        UNIQUE (archive_no, version, stage)
    )
    """,
    # 已登记恢复依据（整包数据的一部分，随包完整迁移）
    """
    CREATE TABLE IF NOT EXISTS recovery_basis (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        archive_no   TEXT NOT NULL,
        version      TEXT NOT NULL,
        basis_no     TEXT NOT NULL,
        basis_type   TEXT,
        content      TEXT,
        payload_hash TEXT,
        UNIQUE (archive_no, version, basis_no)
    )
    """,
    # 迁移恢复凭证
    """
    CREATE TABLE IF NOT EXISTS migration_credentials (
        archive_no    TEXT PRIMARY KEY,
        version       TEXT NOT NULL,
        credential    TEXT NOT NULL,
        issued_at     TEXT NOT NULL,
        expires_at    TEXT
    )
    """,
    # 恢复请求（原接口按原编号继续领取；同一恢复只允许成功办理一次）
    """
    CREATE TABLE IF NOT EXISTS restore_requests (
        request_no     TEXT PRIMARY KEY,
        archive_no     TEXT NOT NULL,
        version        TEXT NOT NULL,
        idempotency_key TEXT,
        stage          TEXT NOT NULL,
        request_status TEXT NOT NULL,
        credential     TEXT,
        credential_valid INTEGER NOT NULL DEFAULT 0,
        data_source    TEXT,
        storage_version TEXT,
        sample_version TEXT,
        slice_batch    TEXT,
        result_summary TEXT,
        idempotent_hit INTEGER NOT NULL DEFAULT 0,
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL,
        UNIQUE (idempotency_key)
    )
    """,
    # 恢复处理事件：可查到请求所处阶段与实际数据源
    """
    CREATE TABLE IF NOT EXISTS restore_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        request_no  TEXT NOT NULL,
        event_type  TEXT NOT NULL,
        from_stage  TEXT,
        to_stage    TEXT,
        data_source TEXT,
        note        TEXT,
        recorded_at TEXT NOT NULL
    )
    """,
    # 切片恢复明细：可核对原归档号与样本版次
    """
    CREATE TABLE IF NOT EXISTS slice_recoveries (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        request_no      TEXT NOT NULL,
        slice_batch     TEXT NOT NULL UNIQUE,
        archive_no      TEXT NOT NULL,
        version         TEXT NOT NULL,
        exp_code        TEXT,
        storage_version TEXT NOT NULL,
        sample_version  TEXT NOT NULL,
        data_source     TEXT NOT NULL,
        compute_input_json TEXT NOT NULL,
        metric_value    REAL,
        evidence_json   TEXT NOT NULL,
        recover_status  TEXT NOT NULL,
        created_at      TEXT NOT NULL
    )
    """,
    # 部署/回滚事件：重启或回滚后仍能说明处理结果
    """
    CREATE TABLE IF NOT EXISTS deployment_journal (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        deployment_no  TEXT NOT NULL,
        action         TEXT NOT NULL,
        result         TEXT NOT NULL,
        note           TEXT,
        recorded_at    TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pkg_exp ON migration_packages(exp_code)",
    "CREATE INDEX IF NOT EXISTS idx_journal_pkg ON package_journal(archive_no, version)",
    "CREATE INDEX IF NOT EXISTS idx_restore_pkg ON restore_requests(archive_no, version)",
    "CREATE INDEX IF NOT EXISTS idx_slice_pkg ON slice_recoveries(archive_no, version)",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or settings.archive_db_path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    with conn:
        for stmt in DDL:
            conn.execute(stmt)
        # 轻量列迁移：旧库已存在时补齐后续版本新增列（SQLite 无 IF NOT EXISTS 列语法）。
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(migration_packages)")}
        if "source_payload_json" not in existing_cols:
            conn.execute("ALTER TABLE migration_packages ADD COLUMN source_payload_json TEXT")
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)
