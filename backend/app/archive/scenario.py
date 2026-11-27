"""A17 场景引导：把系统置为题目描述的初始现场。

现场：
- 旧库（进程内存）存在原归档号 A17 的 v2 归档（正在迁出），以及同实验编码的 v1 版次，
  用来验证"不能凭相同实验编码合并不同归档版次"。
- 新库已写入 A17 摘要（停在摘要阶段），尚未确认全包。
- 一个已受理的恢复请求与上线切换冲突，中途执行了回滚（部署流水可查，请求未办结）。

该模块同时被 E2E 脚本（HTTP 重置接口）和单元级测试调用，保证可重复演示。
"""
from __future__ import annotations

import json
from typing import Any

from app.archive import models
from app.archive.deployment import DeploymentService
from app.archive.engine import get_engine, init_engine
from app.archive.legacy import LEGACY_ARCHIVE_NO_FIELD, LEGACY_STORAGE_FIELD, LEGACY_VERSION_FIELD
from app.archive.repository import ArchiveRepository
from app.archive.restore import RestoreService
from app.store import store

ARCHIVE_A17 = "A17"
EXP_CODE_A17 = "EXP-RETENTION-77"


def _legacy_rows() -> list[dict[str, Any]]:
    """构造旧库 A17 的两个版次：v1（已归档旧版）与 v2（正在迁出，存储版本 v2）。"""
    common = {
        "status": "已归档", "pending": False, "abnormal": False,
        "实验编码": EXP_CODE_A17, "结束时间": "2026-10-12",
    }
    v1_sample = {
        "sampleVersion": "v1-sample-20261001", "ctrlMean": 0.120, "treatMean": 0.132,
        "effectWeight": 1.0, "note": "v1 历史样本",
    }
    v2_sample = {
        "sampleVersion": "v2-sample-20261012", "ctrlMean": 0.124, "treatMean": 0.151,
        "effectWeight": 1.0, "note": "v2 归档存储实际计算输入",
    }
    v1: dict[str, Any] = {
        **common,
        LEGACY_ARCHIVE_NO_FIELD: ARCHIVE_A17, LEGACY_VERSION_FIELD: "v1",
        LEGACY_STORAGE_FIELD: models.STORAGE_V1,
        "结论摘要": "A17 留存实验 v1 初版结论",
        "归档位置": "archive/A17/v1", "保留等级": "标准",
        # v1 故意缺少部分指标字段，验证缺失源字段可查。
        "指标快照": json.dumps({"核心指标": {"留存率": 0.132}}, ensure_ascii=False),
        "恢复依据登记": json.dumps([
            {"basisNo": "BASIS-A17-V1-1", "basisType": "显著性检验", "content": "v1 p=0.041"}],
            ensure_ascii=False),
        "样本切片输入": json.dumps(v1_sample, ensure_ascii=False),
        "应迁移字段清单": json.dumps({
            "summary": ["结论摘要", "结束时间", "保留等级"],
            "metrics": ["核心指标", "样本量核对", "置信区间"],
            "basis": ["恢复依据登记"],
        }, ensure_ascii=False),
    }
    v2: dict[str, Any] = {
        **common,
        LEGACY_ARCHIVE_NO_FIELD: ARCHIVE_A17, LEGACY_VERSION_FIELD: "v2",
        LEGACY_STORAGE_FIELD: models.STORAGE_V2,
        # v2 摘要缺少"保留等级"源字段，验证缺失材料登记。
        "结论摘要": "A17 留存实验 v2 复核结论：实验组留存显著提升",
        "归档位置": "archive/A17/v2",
        "指标快照": json.dumps({
            "核心指标": {"留存率": 0.151},
            "样本量核对": {"对照组": 5120, "实验组": 5118},
            "置信区间": "[0.018, 0.036]",
        }, ensure_ascii=False),
        "恢复依据登记": json.dumps([
            {"basisNo": "BASIS-A17-V2-1", "basisType": "显著性检验", "content": "v2 p=0.003"},
            {"basisNo": "BASIS-A17-V2-2", "basisType": "归因分析", "content": "新手引导贡献 62%"},
        ], ensure_ascii=False),
        "样本切片输入": json.dumps(v2_sample, ensure_ascii=False),
        "应迁移字段清单": json.dumps({
            "summary": ["结论摘要", "结束时间", "保留等级"],
            "metrics": ["核心指标", "样本量核对", "置信区间"],
            "basis": ["恢复依据登记"],
        }, ensure_ascii=False),
    }
    return [v1, v2]


def seed_legacy_a17() -> None:
    """把 A17 两个版次写入旧库（进程内存），清除同号旧数据保证可重复。"""
    rows = store.rows("exp_archive")
    for row in [r for r in rows if r.get(LEGACY_ARCHIVE_NO_FIELD) == ARCHIVE_A17]:
        rows.remove(row)
    next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    for row in _legacy_rows():
        row = dict(row)
        row["id"] = next_id
        next_id += 1
        rows.append(row)


def reset_sqlite(db_path: str | None = None) -> ArchiveRepository:
    """重建迁移库到 A17 初始现场（摘要已写、未确认全包；一个未办结恢复；一次回滚）。"""
    if db_path:
        engine = init_engine(db_path)
    else:
        engine = get_engine()
        # 清空并重建全部表，保证 E2E 可重复进入同一场景。
        for table in [
            "slice_recoveries", "restore_events", "restore_requests", "migration_credentials",
            "recovery_basis", "package_journal", "migration_packages", "deployment_journal",
            "schema_meta",
        ]:
            engine.execute(f"DELETE FROM {table}")
    repo = ArchiveRepository(engine)

    # 1) 仅写入 A17 v2 摘要阶段（未确认全包）——模拟"已写入摘要但尚未确认全包"。
    legacy = _legacy_rows()
    v2 = next(r for r in legacy if r[LEGACY_VERSION_FIELD] == "v2")
    metrics = json.loads(v2["指标快照"])
    repo.upsert_package_start(
        archive_no=ARCHIVE_A17, version="v2", exp_code=EXP_CODE_A17,
        storage_version=models.STORAGE_V2, total_stages=len(models.MIGRATION_STAGES),
        summary=v2["结论摘要"], metrics=metrics, missing_fields=["summary.保留等级"],
        evidence={"source": "legacy-in-memory", "storageVersion": models.STORAGE_V2},
        payload_hash=models.stable_hash({"partial": "summary-only"}),
    )
    repo.upsert_journal(
        archive_no=ARCHIVE_A17, version="v2", stage=models.STAGE_SUMMARY,
        stage_status="已完成(含缺失)", detail={"missing": ["summary.保留等级"]})

    # 2) 受理一个恢复请求（与上线切换冲突），停在"已接收"未办结。
    restore = RestoreService(repo)
    submit = restore.submit_restore(
        archive_no=ARCHIVE_A17, version="v2", idempotency_key="idem-a17-switch-001")
    request_no = submit["request"]["requestNo"]  # type: ignore[index]

    # 3) 上线切换冲突后执行回滚：登记部署流水，请求保持未办结、现场可续。
    deployment = DeploymentService(repo, restore)
    deployment.record(action="上线", note="A17 迁移期间尝试上线切换，与进行中的恢复冲突")
    deployment.record(action="切换失败", note="切换检测到未确认全包与未办结恢复，终止切换")
    deployment.record(action="回滚", note="回滚到旧库版本，恢复请求保留现场")

    return repo


def bootstrap_a17_scenario(db_path: str | None = None) -> dict[str, Any]:
    seed_legacy_a17()
    repo = reset_sqlite(db_path)
    engine = repo.engine
    open_request = repo.find_open_by_archive(ARCHIVE_A17)
    return {
        "ok": True,
        "message": "A17 场景已就绪：摘要已写未确认全包，存在未办结恢复，且已记录一次回滚",
        "dbPath": engine.db_path,
        "archiveNo": ARCHIVE_A17,
        "openRequestNo": open_request["request_no"] if open_request else None,
    }
