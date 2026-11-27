"""旧库适配器：从进程内存归档（旧库 v1）按原归档号读取整包源数据。

迁移与恢复都通过这里访问旧库，避免领域逻辑直接依赖内存 Store 的表结构。
旧库行内结构化字段（指标、恢复依据、样本、缺失清单）以 JSON 字符串存放；
读不到时返回 None，由上层决定是记为"缺失材料"还是走兼容取数。
"""
from __future__ import annotations

import json
from typing import Any

from app.store import store

LEGACY_ARCHIVE_NO_FIELD = "原归档号"
LEGACY_VERSION_FIELD = "归档版次"
LEGACY_STORAGE_FIELD = "归档存储版本"


def _loads(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


class LegacyArchiveSource:
    """旧库（进程内存）只读访问。"""

    def list_archives(self) -> list[dict[str, Any]]:
        return [
            row for row in store.rows("exp_archive")
            if row.get(LEGACY_ARCHIVE_NO_FIELD)
        ]

    def get_archive(self, archive_no: str, version: str | None = None) -> dict[str, Any] | None:
        candidates = [
            row for row in self.list_archives()
            if str(row.get(LEGACY_ARCHIVE_NO_FIELD)) == archive_no
        ]
        if not candidates:
            return None
        if version is not None:
            matched = next((r for r in candidates if str(r.get(LEGACY_VERSION_FIELD)) == version), None)
            if matched is not None:
                return matched
        # 未指定版次时取版次最大者（不与其它归档/版次合并）。
        return sorted(candidates, key=lambda r: str(r.get(LEGACY_VERSION_FIELD, "")))[-1]

    def get_archive_versions(self, archive_no: str) -> list[dict[str, Any]]:
        return [
            row for row in self.list_archives()
            if str(row.get(LEGACY_ARCHIVE_NO_FIELD)) == archive_no
        ]

    # ---- 整包三段源数据 ----

    def read_summary(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "archiveNo": row.get(LEGACY_ARCHIVE_NO_FIELD),
            "version": str(row.get(LEGACY_VERSION_FIELD, "")),
            "expCode": row.get("实验编码", ""),
            "endedAt": row.get("结束时间", ""),
            "conclusion": row.get("结论摘要", ""),
            "retentionLevel": row.get("保留等级", ""),
            "archiveLocation": row.get("归档位置", ""),
        }

    def read_metrics(self, row: dict[str, Any]) -> dict[str, Any]:
        return _loads(row.get("指标快照"), {})

    def read_recovery_basis(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        return _loads(row.get("恢复依据登记"), [])

    def read_expected_fields(self, row: dict[str, Any]) -> dict[str, list[str]]:
        return _loads(row.get("应迁移字段清单"), {
            "summary": ["结论摘要", "结束时间", "保留等级"],
            "metrics": ["核心指标", "样本量核对", "置信区间"],
            "basis": ["恢复依据登记"],
        })

    def read_sample(self, row: dict[str, Any]) -> dict[str, Any]:
        """切片实际计算输入（v2 及其兼容数据）。"""
        return read_sample(row)

    def read_sample_version(self, row: dict[str, Any]) -> str:
        return read_sample_version(row)

    def snapshot_source(self, row: dict[str, Any]) -> dict[str, Any]:
        """把旧库整行快照成 JSON 安全结构，随整包迁移落库，支持重启后独立恢复。"""
        snapshot: dict[str, Any] = {}
        for key, value in row.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                snapshot[key] = value
            else:
                snapshot[key] = json.dumps(value, ensure_ascii=False)
        return snapshot


def read_sample(row: dict[str, Any]) -> dict[str, Any]:
    """切片实际计算输入（v2 及其兼容数据）；无状态，可对 SQLite 还原的源行直接使用。"""
    return _loads(row.get("样本切片输入"), {})


def read_sample_version(row: dict[str, Any]) -> str:
    sample = read_sample(row)
    return str(sample.get("sampleVersion") or f"{row.get(LEGACY_STORAGE_FIELD, 'v1')}-sample")
