"""归档迁移与恢复接口：迁移进度、整包迁移、恢复受理/续办、切片证据、部署流水。

路径约定：
- 全部以 /api/archive-migration 为前缀，与通用资源路由 /api/{module} 并列。
- 对外统一使用"原归档号"查询与受理，原接口语义保持不变。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.archive.deployment import DeploymentService
from app.archive.migration import MigrationService
from app.archive.models import serialize_slice
from app.archive.repository import ArchiveRepository
from app.archive.restore import RestoreService
from app.archive.scenario import bootstrap_a17_scenario

router = APIRouter(prefix="/api/archive-migration", tags=["归档迁移与恢复"])


def _services() -> tuple[MigrationService, RestoreService, DeploymentService, ArchiveRepository]:
    repo = ArchiveRepository()
    return MigrationService(repo), RestoreService(repo), DeploymentService(repo), repo


class MigratePayload(BaseModel):
    archiveNo: str
    version: str | None = None


class RestoreSubmitPayload(BaseModel):
    archiveNo: str
    version: str | None = None
    idempotencyKey: str | None = None
    credential: str | None = None


class ContinuePayload(BaseModel):
    requestNo: str


class DeploymentPayload(BaseModel):
    action: str
    note: str = ""
    deploymentNo: str | None = None


class CredentialPayload(BaseModel):
    archiveNo: str


@router.get("/packages/{archive_no}/progress")
def migration_progress(archive_no: str) -> dict[str, Any]:
    """按旧编号查迁移进度与缺失材料。"""
    migration, _, _, _ = _services()
    return migration.progress(archive_no)


@router.post("/packages/migrate")
def migrate_package(payload: MigratePayload) -> dict[str, Any]:
    """服务端按原归档号整包迁移（可重复调用，已确认则幂等返回）。"""
    migration, _, _, _ = _services()
    result = migration.migrate_archive(payload.archiveNo, payload.version)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result["message"])
    return result


@router.post("/credentials")
def issue_credential(payload: CredentialPayload) -> dict[str, Any]:
    """提供/补签迁移恢复凭证。"""
    migration, _, _, _ = _services()
    result = migration.issue_credential(payload.archiveNo)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result["message"])
    return result


@router.post("/restores")
def submit_restore(payload: RestoreSubmitPayload) -> dict[str, Any]:
    """原接口使用原编号受理恢复（幂等：同归档未办结/已办结都不会重复执行）。"""
    _, restore, _, _ = _services()
    result = restore.submit_restore(
        archive_no=payload.archiveNo, version=payload.version,
        idempotency_key=payload.idempotencyKey, credential=payload.credential)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result["message"])
    return result


@router.post("/restores/continue")
def continue_restore(payload: ContinuePayload) -> dict[str, Any]:
    """从原请求继续办理未完成恢复（重启/回滚后可反复调用，已办结不二次执行）。"""
    _, restore, _, _ = _services()
    result = restore.continue_restore(payload.requestNo)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result["message"])
    return result


@router.get("/restores")
def list_restores(archiveNo: str | None = None) -> dict[str, Any]:
    _, restore, _, _ = _services()
    items = restore.list_requests(archiveNo)
    return {"items": items, "total": len(items)}


@router.get("/restores/unfinished")
def list_unfinished() -> dict[str, Any]:
    _, restore, _, _ = _services()
    items = restore.list_unfinished()
    return {"items": items, "total": len(items)}


@router.get("/restores/{request_no}")
def get_restore(request_no: str) -> dict[str, Any]:
    """查恢复请求所处阶段、事件流水、实际数据源与切片证据。"""
    _, restore, _, _ = _services()
    result = restore.get_request(request_no)
    if result is None:
        raise HTTPException(status_code=404, detail=f"恢复请求 {request_no} 不存在")
    return result


@router.get("/slices")
def list_slices(archiveNo: str | None = None) -> dict[str, Any]:
    _, _, _, repo = _services()
    items = [serialize_slice(r) for r in repo.list_slice_recoveries(archiveNo)]
    return {"items": items, "total": len(items)}


@router.get("/slices/{slice_batch}")
def get_slice(slice_batch: str) -> dict[str, Any]:
    """切片明细：核对原归档号、样本版次、归档存储版本与证据。"""
    _, restore, _, _ = _services()
    result = restore.get_slice_detail(slice_batch)
    if result is None:
        raise HTTPException(status_code=404, detail=f"切片批次 {slice_batch} 不存在")
    return result


@router.post("/deployments")
def record_deployment(payload: DeploymentPayload) -> dict[str, Any]:
    _, _, deployment, _ = _services()
    result = deployment.record(action=payload.action, note=payload.note,
                               deployment_no=payload.deploymentNo)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.get("/deployments")
def list_deployments() -> dict[str, Any]:
    _, _, deployment, _ = _services()
    items = deployment.journal()
    return {"items": items, "total": len(items)}


@router.post("/startup-recover")
def startup_recover() -> dict[str, Any]:
    """服务重启后续办未完成恢复；已办结恢复不会被执行第二次。"""
    _, _, deployment, _ = _services()
    return deployment.startup_recover()


@router.post("/scenario/bootstrap")
def bootstrap_scenario() -> dict[str, Any]:
    """测试/演示：重建 A17 初始现场（摘要已写未确认、未办结恢复、一次回滚）。"""
    return bootstrap_a17_scenario()
