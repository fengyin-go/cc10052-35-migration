"""实验指标分析与归因评估平台 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8022
健康检查：GET /api/health
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.archive.engine import get_engine
from app.config import settings
from app.routers import ROUTERS
from app.store import store


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动即建表，保证部署上线后立刻能查迁移进度、续办未完成恢复。

    自动续办默认开启；测试场景需要保留现场时用 ARCHIVE_AUTO_RECOVERY=0 关闭，
    改为显式调用 /api/archive-migration/startup-recover。
    """
    get_engine()  # 触发 SQLite 建表
    if os.environ.get("ARCHIVE_AUTO_RECOVERY", "1") == "1":
        try:
            from app.archive.deployment import DeploymentService
            DeploymentService().startup_recover()
        except Exception:  # pragma: no cover - 启动容错，不阻断服务
            # 续办失败不应阻止服务启动；现场仍持久化，可稍后手工续办。
            pass
    yield


app = FastAPI(title="实验指标分析与归因评估平台", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、示例数据已经就绪。"""
    return {"ok": True, "app": settings.app_name, "modules": len(store.module_names())}


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    return store.overview()


# 资源路由含 /api/{module}，必须放在 health / overview 与固定迁移路径之后注册，
# 否则固定路径会被动态路径抢先匹配。
for module in ROUTERS:
    app.include_router(module.router)
