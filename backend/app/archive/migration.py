"""迁移服务：按原归档号把一个归档版次整包迁移到 SQLite，并签发迁移恢复凭证。

不变量：
- 以 (原归档号, 归档版次) 为单位整包迁移；绝不因实验编码相同而合并不同版次。
- 摘要、指标、已登记恢复依据作为完整一份数据迁移，缺字段只登记到缺失材料，
  不静默丢弃；每个阶段都写流水，重启后从最后阶段继续，不重复已完成阶段。
"""
from __future__ import annotations

from typing import Any

from app.archive import models
from app.archive.credential import issue_credential
from app.archive.legacy import LEGACY_ARCHIVE_NO_FIELD, LegacyArchiveSource
from app.archive.repository import ArchiveRepository


class MigrationService:
    def __init__(
        self,
        repository: ArchiveRepository | None = None,
        legacy: LegacyArchiveSource | None = None,
    ) -> None:
        self.repo = repository or ArchiveRepository()
        self.legacy = legacy or LegacyArchiveSource()

    # ---------------- 查询 ----------------

    def progress(self, archive_no: str) -> dict[str, Any]:
        """按旧编号查迁移进度与缺失材料（含该编号下全部版次，不合并）。"""
        legacy_rows = self.legacy.get_archive_versions(archive_no)
        packages = [
            self._package_view(p) for p in self.repo.list_packages()
            if p["archive_no"] == archive_no
        ]
        seen_versions = {p["version"] for p in packages}
        for legacy_row in legacy_rows:
            version = str(legacy_row.get("归档版次", ""))
            if version not in seen_versions:
                packages.append({
                    "archiveNo": archive_no,
                    "version": version,
                    "packageStatus": "未开始",
                    "stage": "未开始",
                    "finishedStages": 0,
                    "totalStages": len(models.MIGRATION_STAGES),
                    "missingFields": [],
                    "journal": [],
                })
        credential = self.repo.get_credential(archive_no)
        return {
            "archiveNo": archive_no,
            "versions": packages,
            "credential": credential.get("credential") if credential else None,
        }

    def _package_view(self, package: dict[str, Any]) -> dict[str, Any]:
        journal = self.repo.list_journal(package["archive_no"], package["version"])
        package["_journal"] = [
            {"stage": j["stage"], "status": j["stage_status"],
             "detail": models._loads(j["detail"], {}), "recordedAt": j["recorded_at"]}
            for j in journal
        ]
        return models.serialize_package(package)

    # ---------------- 迁移执行 ----------------

    def migrate_archive(self, archive_no: str, version: str | None = None) -> dict[str, Any]:
        legacy_row = self.legacy.get_archive(archive_no, version)
        if legacy_row is None:
            return {"ok": False, "message": f"旧库找不到原归档号 {archive_no} 的归档包"}
        version = str(legacy_row.get("归档版次", ""))
        exp_code = str(legacy_row.get("实验编码", ""))
        storage_version = str(legacy_row.get("归档存储版本", models.STORAGE_V1))

        with self.repo.engine.write_lock:
            existing = self.repo.get_package(archive_no, version)
            if existing and existing["package_status"] == models.PACKAGE_CONFIRMED:
                # 已确认全包：幂等返回，不重复迁移。
                return {"ok": True, "message": "归档包已确认全包，无需重复迁移",
                        "package": self._package_view(existing), "resumed": False}

            summary_part = self._migrate_summary(legacy_row, existing, storage_version)
            metrics_part = self._migrate_metrics(legacy_row)
            basis_part = self._migrate_basis(legacy_row)

            missing = summary_part["missing"] + metrics_part["missing"] + basis_part["missing"]
            full_payload = {
                "summary": summary_part["data"],
                "metrics": metrics_part["data"],
                "recoveryBasis": basis_part["data"],
            }
            payload_hash = models.stable_hash(full_payload)

            if existing is None:
                package = self.repo.upsert_package_start(
                    archive_no=archive_no, version=version, exp_code=exp_code,
                    storage_version=storage_version, total_stages=len(models.MIGRATION_STAGES),
                    summary=summary_part["data"].get("conclusion"),
                    metrics=metrics_part["data"], missing_fields=missing,
                    evidence={"source": "legacy-in-memory", "storageVersion": storage_version},
                    payload_hash=payload_hash,
                )
            else:
                self.repo.update_package_stage(
                    archive_no, version, stage=models.STAGE_SUMMARY,
                    status=models.PACKAGE_IN_PROGRESS,
                    finished_stages=existing.get("finished_stages", 0))

            # 阶段流水（已存在则更新，重启续跑不重复计费）。
            self.repo.upsert_journal(
                archive_no=archive_no, version=version, stage=models.STAGE_SUMMARY,
                stage_status=summary_part["stage_status"],
                detail={"missing": summary_part["missing"], "hash": models.stable_hash(summary_part["data"])})
            self.repo.upsert_journal(
                archive_no=archive_no, version=version, stage=models.STAGE_METRICS,
                stage_status=metrics_part["stage_status"],
                detail={"missing": metrics_part["missing"], "hash": models.stable_hash(metrics_part["data"])})
            self.repo.upsert_journal(
                archive_no=archive_no, version=version, stage=models.STAGE_BASIS,
                stage_status=basis_part["stage_status"],
                detail={"count": len(basis_part["data"]),
                        "hash": models.stable_hash(basis_part["data"])})

            # 整包数据完整写入后确认全包（即使有缺字段，也已在缺失材料中可查）。
            self.repo.save_source_payload(
                archive_no, version, self.legacy.snapshot_source(legacy_row))
            self.repo.mark_package_confirmed(archive_no, version)
            self._ensure_credential(archive_no, version)
            package = self.repo.get_package(archive_no, version)
            return {
                "ok": True,
                "resumed": existing is not None,
                "message": "整包迁移完成并确认全包" + (f"，缺失字段 {len(missing)} 项已登记" if missing else ""),
                "package": self._package_view(package),  # type: ignore[arg-type]
                "missingFields": missing,
                "credential": self.repo.get_credential(archive_no)["credential"],
            }

    def _migrate_summary(
        self, legacy_row: dict[str, Any], existing: dict[str, Any] | None, storage_version: str,
    ) -> dict[str, Any]:
        data = self.legacy.read_summary(legacy_row)
        expected = self.legacy.read_expected_fields(legacy_row).get("summary", [])
        present = set(legacy_row.keys())
        missing = [f for f in expected if f not in present or legacy_row.get(f) in (None, "")]
        return {
            "data": data,
            "missing": [f"summary.{f}" for f in missing],
            "stage_status": "已完成(含缺失)" if missing else "已完成",
        }

    def _migrate_metrics(self, legacy_row: dict[str, Any]) -> dict[str, Any]:
        data = self.legacy.read_metrics(legacy_row)
        expected = self.legacy.read_expected_fields(legacy_row).get("metrics", [])
        missing = [f"metrics.{f}" for f in expected if not _nested_has(data, f)]
        return {
            "data": data,
            "missing": missing,
            "stage_status": "已完成(含缺失)" if missing else "已完成",
        }

    def _migrate_basis(self, legacy_row: dict[str, Any]) -> dict[str, Any]:
        basis = self.legacy.read_recovery_basis(legacy_row)
        expected = self.legacy.read_expected_fields(legacy_row).get("basis", [])
        missing: list[str] = []
        if not basis and "恢复依据登记" in expected:
            missing.append("basis.恢复依据登记")
        # 已登记恢复依据作为整包的一部分完整落库。
        self.repo.replace_recovery_basis(
            archive_no=str(legacy_row[LEGACY_ARCHIVE_NO_FIELD]),
            version=str(legacy_row.get("归档版次", "")),
            basis_list=[
                {
                    "basis_no": b.get("basisNo", f"BASIS-{idx + 1}"),
                    "basis_type": b.get("basisType", ""),
                    "content": b.get("content", ""),
                    "payload_hash": models.stable_hash(b),
                }
                for idx, b in enumerate(basis)
            ],
        )
        return {"data": basis, "missing": missing,
                "stage_status": "已完成(含缺失)" if missing else "已完成"}

    def _ensure_credential(self, archive_no: str, version: str) -> None:
        if self.repo.get_credential(archive_no) is None:
            credential, expires = issue_credential(archive_no=archive_no, version=version)
            self.repo.save_credential(
                archive_no=archive_no, version=version, credential=credential, expires_at=expires)

    def issue_credential(self, archive_no: str) -> dict[str, Any]:
        package = self.repo.get_package_by_archive_no(archive_no)
        if package is None:
            return {"ok": False, "message": "迁移库中没有该归档，无法签发凭证"}
        credential, expires = issue_credential(
            archive_no=archive_no, version=package["version"])
        self.repo.save_credential(
            archive_no=archive_no, version=package["version"],
            credential=credential, expires_at=expires)
        return {"ok": True, "credential": credential, "expiresAt": expires,
                "archiveNo": archive_no, "version": package["version"]}


def _nested_has(data: dict[str, Any], key: str) -> bool:
    if not isinstance(data, dict):
        return False
    if key in data and data[key] not in (None, "", [], {}):
        return True
    return any(_nested_has(v, key) for v in data.values() if isinstance(v, dict))
