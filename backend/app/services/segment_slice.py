"""切片分析器领域服务：管理实验差异的维度切片、切片值计算与失败重试。

数据链路位置：下钻分析层，消费显著性检验与归因分析的差异结果，按维度切片
计算分组差异，输出给报告生成器形成下钻章节。

归档恢复的切片以 SQLite 迁移库为准（单一事实来源）；本服务在列表/明细查询时
合并恢复后的切片，使其在原"切片分析器"页面可见，且能核对原归档号与样本版次。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


def _recovered_slice_rows() -> list[dict[str, Any]]:
    """从迁移库读取已恢复切片，转成切片分析器列表行（不回写内存，避免双写）。"""
    try:
        from app.archive.repository import ArchiveRepository
    except Exception:
        return []
    repo = ArchiveRepository()
    rows: list[dict[str, Any]] = []
    for record in repo.list_slice_recoveries():
        evidence = record.get("evidence_json")
        rows.append({
            "id": 10_000 + record["id"],
            "切片批次": record["slice_batch"],
            "实验编码": record.get("exp_code", ""),
            "切片维度": "归档恢复",
            "切片值": f"原归档号 {record['archive_no']} / 样本版次 {record['sample_version']}",
            "分组差异": str(record.get("metric_value", "")),
            "status": "已计算" if record.get("recover_status") == "已恢复" else record.get("recover_status", ""),
            "pending": False,
            "abnormal": False,
            "原归档号": record["archive_no"],
            "归档版次": record["version"],
            "归档存储版本": record["storage_version"],
            "样本版次": record["sample_version"],
            "实际数据源": record["data_source"],
            "恢复证据": evidence,
        })
    return rows


class SegmentSliceService(BaseModuleService):
    MODULE = "segment_slice"
    ENTITY = "切片结果"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["切片批次", "实验编码", "切片维度", "切片值", "分组差异"]
    REQUIRED_FIELDS = ["切片批次", "实验编码", "切片维度"]
    STATUS_ORDER = ["待计算", "已计算", "计算失败"]
    ACTION_RULES = {"开始计算": "已计算", "重试计算": "计算失败"}
    NEGATIVE_ACTIONS = []

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        # 合并迁移库中恢复的切片，再套用通用筛选/分页。
        merged = list(store.rows(self.MODULE)) + _recovered_slice_rows()
        for field, expected in (filters or {}).items():
            if field in self.LIST_FIELDS:
                merged = [
                    row for row in merged
                    if expected.casefold() in str(row.get(field, "")).casefold()
                ]
        if keyword and self.LIST_FIELDS:
            field = self.LIST_FIELDS[0]
            merged = [row for row in merged if keyword.casefold() in str(row.get(field, "")).casefold()]
        if status:
            merged = [row for row in merged if row.get("status") == status]
        total = len(merged)
        start = (page - 1) * size
        return merged[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        local = super().get_entry(entry_id)
        if local is not None:
            return local
        for row in _recovered_slice_rows():
            if int(row.get("id", 0)) == entry_id:
                return row
        return None


    # ---- 业务扩展：维度切片与差异计算 ----

    def run_slice(self, entry_id: int) -> dict[str, Any]:
        """执行切片计算：按维度聚合分组差异并写回结果。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "待计算":
            return {"ok": False, "message": "仅待计算切片可执行"}
        exp_code = str(entry.get("实验编码", ""))
        dimension = str(entry.get("切片维度", ""))
        assign_rows = [
            r for r in store.rows("variant_assign")
            if r.get("实验编码") == exp_code and r.get("status") != "已剔除"
        ]
        if not assign_rows:
            entry["status"] = "计算失败"
            entry["abnormal"] = True
            entry["失败原因"] = "无分组数据可切片"
            return {"ok": False, "message": "实验无分组数据，切片计算失败"}
        if dimension not in ("分组", "人群", "归档恢复"):
            entry["status"] = "计算失败"
            entry["abnormal"] = True
            entry["失败原因"] = f"不支持的切片维度 {dimension}"
            return {"ok": False, "message": f"切片维度 {dimension} 不受支持"}
        ctrl = sum(1 for r in assign_rows if r.get("分组名称") == "对照组")
        treat = sum(1 for r in assign_rows if r.get("分组名称") == "实验组")
        diff = round((treat - ctrl) / max(len(assign_rows), 1), 4)
        entry["分组差异"] = str(diff)
        entry["status"] = "已计算"
        entry["pending"] = False
        entry["计算时间"] = datetime.now(timezone.utc).isoformat()
        return {"ok": True, "message": f"切片 {entry.get('切片值', dimension)} 差异 {diff} 已计算"}

    def retry_failed(self, entry_id: int) -> dict[str, Any]:
        """重试失败切片：修复失败原因后回到待计算。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "计算失败":
            return {"ok": False, "message": "仅计算失败切片可重试"}
        reason = str(entry.get("失败原因", ""))
        if "维度" in reason:
            entry["切片维度"] = "分组"
        entry["status"] = "待计算"
        entry["pending"] = True
        entry["abnormal"] = False
        entry["重试次数"] = str(int(entry.get("重试次数", "0") or 0) + 1)
        return {"ok": True, "message": f"切片已修复并回到待计算（第 {entry['重试次数']} 次）"}

    def batch_run_pending(self) -> dict[str, Any]:
        """批量执行全部待计算切片。"""
        pending = [r for r in store.rows(self.MODULE) if r.get("status") == "待计算"]
        done = sum(1 for r in pending if self.run_slice(int(r.get("id", 0))).get("ok"))
        return {"ok": True, "message": f"批量切片完成 {done}/{len(pending)}"}

    def consume_from_significance_test(self, exp_code: str) -> list[dict[str, Any]]:
        """读取显著性检验的置信区间，作为切片差异对照基线。"""
        return [
            {"指标编码": r.get("指标编码", ""), "置信区间": r.get("置信区间", "")}
            for r in store.rows("significance_test")
            if r.get("实验编码") == exp_code and r.get("status") == "已检验"
        ]

    def consume_from_cohort_builder(self, cohort_code: str) -> dict[str, Any]:
        """读取人群圈选器的人群包信息，限定切片范围。"""
        cohort = next((r for r in store.rows("cohort_builder") if r.get("人群编码") == cohort_code), None)
        if cohort is None:
            return {"ok": False, "message": f"人群包 {cohort_code} 不存在"}
        return {
            "ok": cohort.get("status") == "已生成",
            "人群规模": cohort.get("人群规模", ""),
            "状态": cohort.get("status", ""),
        }

    def produce_to_report_gen(self, exp_code: str) -> dict[str, Any]:
        """把已计算切片推送给报告生成器，登记下钻章节报告。"""
        computed = [
            r for r in store.rows(self.MODULE)
            if r.get("实验编码") == exp_code and r.get("status") == "已计算"
        ]
        if not computed:
            return {"ok": False, "message": "实验无已计算切片"}
        report_rows = store.rows("report_gen")
        report_rows.append({
            "id": max((int(r.get("id", 0)) for r in report_rows), default=0) + 1,
            "报告编码": f"RPT-SG-{exp_code}-{datetime.now(timezone.utc).timestamp():.0f}",
            "实验编码": exp_code,
            "报告模板": "切片下钻模板",
            "数据窗口": "最近7天",
            "生成状态": "排队中",
            "status": "排队中",
            "pending": True,
            "abnormal": False,
        })
        return {"ok": True, "message": f"下钻报告已排队（覆盖 {len(computed)} 条切片）"}

    def export_slice_matrix(self, exp_code: str) -> list[dict[str, Any]]:
        """导出实验的切片矩阵，供指标查询服务提供下钻数据。"""
        return [
            {
                "切片维度": r.get("切片维度", ""),
                "切片值": r.get("切片值", ""),
                "分组差异": r.get("分组差异", ""),
            }
            for r in store.rows(self.MODULE)
            if r.get("实验编码") == exp_code and r.get("status") == "已计算"
        ]
