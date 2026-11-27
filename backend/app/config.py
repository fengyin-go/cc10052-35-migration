"""运行配置：端口、跨域、运行环境，以及归档迁移 SQLite 持久化配置。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_db_path() -> str:
    # 默认落在后端 data/ 目录，*.sqlite3 已在 .gitignore 中忽略。
    return str(Path(__file__).resolve().parent.parent / "data" / "archive.sqlite3")


@dataclass(frozen=True)
class Settings:
    app_name: str = "实验指标分析与归因评估平台"
    env: str = "local"
    port: int = 8022
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5202",
            "http://localhost:5202",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200
    # 归档迁移库路径：部署时可用环境变量指向持久卷，测试时指向临时库。
    archive_db_path: str = field(default_factory=lambda: os.environ.get("ARCHIVE_DB_PATH", _default_db_path()))
    # 迁移恢复凭证 HMAC 密钥；生产必须通过环境变量注入。
    archive_credential_secret: str = field(
        default_factory=lambda: os.environ.get("ARCHIVE_CREDENTIAL_SECRET", "dev-only-archive-credential-secret")
    )


settings = Settings()
