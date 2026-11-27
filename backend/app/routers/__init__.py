"""业务路由汇总：通用资源路由 + 归档迁移恢复路由。"""
from __future__ import annotations

from app.routers import archive_migration, resource

ROUTERS = [archive_migration, resource]
