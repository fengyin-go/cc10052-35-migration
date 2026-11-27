"""部署/回滚服务：记录上线切换与回滚事件，并在服务启动时继续未办结恢复。

不变量：
- 上线或回滚都不重新执行已办结恢复（恢复只成功一次），只登记事件并说明处理结果。
- 已接收未办结的请求在启动/切换后仍有继续处理依据（持久化阶段 + 事件流水），
  可通过 recover_unfinished 续跑，也可逐条 continue_restore。
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.archive.repository import ArchiveRepository
from app.archive.restore import RestoreService


class DeploymentService:
    def __init__(
        self,
        repository: ArchiveRepository | None = None,
        restore_service: RestoreService | None = None,
    ) -> None:
        self.repo = repository or ArchiveRepository()
        self.restore = restore_service or RestoreService(self.repo)

    def record(self, *, action: str, note: str = "", deployment_no: str | None = None) -> dict[str, object]:
        deployment_no = deployment_no or f"DEP-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
        valid_actions = {"上线", "回滚", "重启", "切换失败"}
        if action not in valid_actions:
            return {"ok": False, "message": f"未知部署动作：{action}"}
        # 回滚/切换失败不改变已办结恢复，只登记结果与影响面。
        unfinished = self.repo.list_restore_requests()
        affected = [r["request_no"] for r in unfinished if r["request_status"] != "已办结"]
        result = "已记录"
        note_full = note or ""
        if action in ("回滚", "切换失败"):
            note_full = (
                f"{note_full}；已办结恢复不重复执行，未办结请求 {len(affected)} 条保留现场可续办"
            ).strip("；")
        row_id = self.repo.add_deployment(
            deployment_no=deployment_no, action=action, result=result, note=note_full)
        return {
            "ok": True, "deploymentId": row_id, "deploymentNo": deployment_no,
            "action": action, "affectedOpenRequests": affected, "note": note_full,
        }

    def startup_recover(self) -> dict[str, object]:
        """服务启动时调用：登记重启并把未办结恢复继续办完（已办结的绝不二次执行）。"""
        self.record(action="重启", note="服务启动，扫描未办结恢复并续办")
        unfinished = self.restore.list_unfinished()
        resumed: list[dict[str, object]] = []
        for item in unfinished:
            outcome = self.restore.continue_restore(item["requestNo"])  # type: ignore[index]
            resumed.append({
                "requestNo": item["requestNo"],  # type: ignore[index]
                "ok": bool(outcome.get("ok")),
                "message": outcome.get("message"),
                "stage": (outcome.get("request") or {}).get("stage"),
            })
        return {"ok": True, "scanned": len(unfinished), "resumed": resumed}

    def journal(self) -> list[dict[str, object]]:
        return [
            {"deploymentNo": d["deployment_no"], "action": d["action"], "result": d["result"],
             "note": d["note"], "recordedAt": d["recorded_at"]}
            for d in self.repo.list_deployments()
        ]
