"""归档迁移领域常量与序列化/摘要工具。"""
from __future__ import annotations

import hashlib
import json
from typing import Any

# 整包迁移的三个阶段：摘要、指标、已登记恢复依据，必须作为完整一份数据迁移。
STAGE_SUMMARY = "摘要已写入"
STAGE_METRICS = "指标已迁移"
STAGE_BASIS = "恢复依据已迁移"
STAGE_CONFIRMED = "全包已确认"
MIGRATION_STAGES = [STAGE_SUMMARY, STAGE_METRICS, STAGE_BASIS]

PACKAGE_IN_PROGRESS = "进行中"
PACKAGE_CONFIRMED = "已确认全包"
PACKAGE_FAILED = "迁移失败"

# 恢复请求阶段（接收 -> 校验凭证 -> 取数 -> 切片恢复 -> 办结）。
RESTORE_STAGE_RECEIVED = "已接收"
RESTORE_STAGE_CREDENTIAL = "凭证已校验"
RESTORE_STAGE_FETCH = "数据源已就绪"
RESTORE_STAGE_SLICE = "切片已恢复"
RESTORE_STAGE_DONE = "已办结"

RESTORE_STATUS_OPEN = "办理中"
RESTORE_STATUS_DONE = "已办结"

STORAGE_V2 = "v2"
STORAGE_V1 = "v1"

# 实际数据源标记：v2 新库 / 兼容旧库。
SOURCE_SQLITE_V2 = "归档存储v2(SQLite迁移库)"
SOURCE_LEGACY_V1 = "兼容旧库(进程内存归档v1)"


def stable_hash(data: Any) -> str:
    """对任意可 JSON 化数据计算稳定 SHA256，用作整包/证据指纹。"""
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _loads(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def serialize_package(row: dict[str, Any]) -> dict[str, Any]:
    """把迁移包行转成对外结构，并解析 JSON 字段、补缺失材料视图。"""
    missing = _loads(row.get("missing_fields_json"), [])
    journal = row.get("_journal")
    return {
        "archiveNo": row.get("archive_no"),
        "version": row.get("version"),
        "expCode": row.get("exp_code"),
        "storageVersion": row.get("storage_version"),
        "stage": row.get("stage"),
        "packageStatus": row.get("package_status"),
        "summary": row.get("summary"),
        "metrics": _loads(row.get("metrics_json"), {}),
        "evidence": _loads(row.get("evidence_json"), {}),
        "payloadHash": row.get("payload_hash"),
        "missingFields": missing,
        "totalStages": row.get("total_stages"),
        "finishedStages": row.get("finished_stages"),
        "createdAt": row.get("created_at"),
        "updatedAt": row.get("updated_at"),
        "journal": journal or [],
    }


def serialize_restore(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "requestNo": row.get("request_no"),
        "archiveNo": row.get("archive_no"),
        "version": row.get("version"),
        "idempotencyKey": row.get("idempotency_key"),
        "stage": row.get("stage"),
        "requestStatus": row.get("request_status"),
        "credentialValid": bool(row.get("credential_valid")),
        "dataSource": row.get("data_source"),
        "storageVersion": row.get("storage_version"),
        "sampleVersion": row.get("sample_version"),
        "sliceBatch": row.get("slice_batch"),
        "resultSummary": row.get("result_summary"),
        "idempotentHit": bool(row.get("idempotent_hit")),
        "createdAt": row.get("created_at"),
        "updatedAt": row.get("updated_at"),
    }


def serialize_slice(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "requestNo": row.get("request_no"),
        "sliceBatch": row.get("slice_batch"),
        "archiveNo": row.get("archive_no"),
        "version": row.get("version"),
        "expCode": row.get("exp_code"),
        "storageVersion": row.get("storage_version"),
        "sampleVersion": row.get("sample_version"),
        "dataSource": row.get("data_source"),
        "computeInput": _loads(row.get("compute_input_json"), {}),
        "metricValue": row.get("metric_value"),
        "evidence": _loads(row.get("evidence_json"), {}),
        "recoverStatus": row.get("recover_status"),
        "createdAt": row.get("created_at"),
    }
