"""归档迁移仓储：集中所有 SQLite 读写，领域服务不直接拼 SQL。"""
from __future__ import annotations

import json
from typing import Any

from app.archive.db import utc_now
from app.archive.engine import ArchiveEngine, get_engine


class ArchiveRepository:
    def __init__(self, engine: ArchiveEngine | None = None) -> None:
        self.engine = engine or get_engine()

    # ---------------- 迁移包 ----------------

    def upsert_package_start(
        self,
        *,
        archive_no: str,
        version: str,
        exp_code: str,
        storage_version: str,
        total_stages: int,
        summary: str | None,
        metrics: dict[str, Any] | None,
        missing_fields: list[str],
        evidence: dict[str, Any],
        payload_hash: str,
        source_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        existing = self.get_package(archive_no, version)
        if existing is None:
            self.engine.execute(
                """
                INSERT INTO migration_packages(
                    archive_no, version, exp_code, storage_version, stage, package_status,
                    summary, metrics_json, evidence_json, source_payload_json, payload_hash,
                    missing_fields_json, total_stages, finished_stages, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    archive_no, version, exp_code, storage_version, "摘要已写入", "进行中",
                    summary, json.dumps(metrics or {}, ensure_ascii=False),
                    json.dumps(evidence, ensure_ascii=False),
                    json.dumps(source_payload or {}, ensure_ascii=False), payload_hash,
                    json.dumps(missing_fields, ensure_ascii=False),
                    total_stages, 0, now, now,
                ),
            )
        return self.get_package(archive_no, version)  # type: ignore[return-value]

    def get_package(self, archive_no: str, version: str) -> dict[str, Any] | None:
        return self.engine.query_one(
            "SELECT * FROM migration_packages WHERE archive_no=? AND version=?",
            (archive_no, version),
        )

    def get_package_by_archive_no(self, archive_no: str) -> dict[str, Any] | None:
        # 同一原归档号取最新版次（版次可比较时取最大，否则取最近更新）。
        rows = self.engine.query_all(
            "SELECT * FROM migration_packages WHERE archive_no=? ORDER BY updated_at DESC",
            (archive_no,),
        )
        return rows[0] if rows else None

    def list_packages(self, exp_code: str | None = None) -> list[dict[str, Any]]:
        if exp_code:
            return self.engine.query_all(
                "SELECT * FROM migration_packages WHERE exp_code=? ORDER BY created_at", (exp_code,))
        return self.engine.query_all("SELECT * FROM migration_packages ORDER BY created_at")

    def update_package_stage(
        self, archive_no: str, version: str, *, stage: str, status: str,
        finished_stages: int,
    ) -> None:
        self.engine.execute(
            "UPDATE migration_packages SET stage=?, package_status=?, finished_stages=?, updated_at=? "
            "WHERE archive_no=? AND version=?",
            (stage, status, finished_stages, utc_now(), archive_no, version),
        )

    def mark_package_confirmed(self, archive_no: str, version: str) -> None:
        self.engine.execute(
            "UPDATE migration_packages SET package_status='已确认全包', stage='全包已确认', "
            "finished_stages=total_stages, updated_at=? WHERE archive_no=? AND version=?",
            (utc_now(), archive_no, version),
        )

    def save_source_payload(self, archive_no: str, version: str, source_payload: dict[str, Any]) -> None:
        """整包迁移时把完整源包持久化，保证进程重启后恢复不再依赖内存旧库。"""
        self.engine.execute(
            "UPDATE migration_packages SET source_payload_json=?, updated_at=? "
            "WHERE archive_no=? AND version=?",
            (json.dumps(source_payload, ensure_ascii=False), utc_now(), archive_no, version),
        )

    # ---------------- 迁移阶段流水 ----------------

    def upsert_journal(
        self, *, archive_no: str, version: str, stage: str, stage_status: str, detail: dict[str, Any],
    ) -> None:
        self.engine.execute(
            """
            INSERT INTO package_journal(archive_no, version, stage, stage_status, detail, recorded_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(archive_no, version, stage) DO UPDATE SET
                stage_status=excluded.stage_status, detail=excluded.detail, recorded_at=excluded.recorded_at
            """,
            (archive_no, version, stage, stage_status, json.dumps(detail, ensure_ascii=False), utc_now()),
        )

    def list_journal(self, archive_no: str, version: str) -> list[dict[str, Any]]:
        return self.engine.query_all(
            "SELECT * FROM package_journal WHERE archive_no=? AND version=? ORDER BY id",
            (archive_no, version),
        )

    # ---------------- 恢复依据 ----------------

    def replace_recovery_basis(
        self, *, archive_no: str, version: str, basis_list: list[dict[str, Any]],
    ) -> None:
        self.engine.execute(
            "DELETE FROM recovery_basis WHERE archive_no=? AND version=?", (archive_no, version))
        rows = [
            (archive_no, version, b.get("basis_no", ""), b.get("basis_type", ""),
             b.get("content", ""), b.get("payload_hash", ""))
            for b in basis_list
        ]
        if rows:
            self.engine.executemany(
                "INSERT INTO recovery_basis(archive_no, version, basis_no, basis_type, content, payload_hash) "
                "VALUES(?,?,?,?,?,?)", rows,
            )

    def list_recovery_basis(self, archive_no: str, version: str) -> list[dict[str, Any]]:
        return self.engine.query_all(
            "SELECT * FROM recovery_basis WHERE archive_no=? AND version=? ORDER BY id",
            (archive_no, version),
        )

    # ---------------- 迁移恢复凭证 ----------------

    def save_credential(
        self, *, archive_no: str, version: str, credential: str, expires_at: str | None,
    ) -> None:
        self.engine.execute(
            """
            INSERT INTO migration_credentials(archive_no, version, credential, issued_at, expires_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(archive_no) DO UPDATE SET
                version=excluded.version, credential=excluded.credential,
                issued_at=excluded.issued_at, expires_at=excluded.expires_at
            """,
            (archive_no, version, credential, utc_now(), expires_at),
        )

    def get_credential(self, archive_no: str) -> dict[str, Any] | None:
        return self.engine.query_one(
            "SELECT * FROM migration_credentials WHERE archive_no=?", (archive_no,))

    # ---------------- 恢复请求 ----------------

    def insert_restore_request(self, row: dict[str, Any]) -> None:
        self.engine.execute(
            """
            INSERT INTO restore_requests(
                request_no, archive_no, version, idempotency_key, stage, request_status,
                credential, credential_valid, data_source, storage_version, sample_version,
                slice_batch, result_summary, idempotent_hit, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row["request_no"], row["archive_no"], row["version"], row.get("idempotency_key"),
                row["stage"], row["request_status"], row.get("credential"),
                int(bool(row.get("credential_valid"))), row.get("data_source"),
                row.get("storage_version"), row.get("sample_version"), row.get("slice_batch"),
                row.get("result_summary"), int(bool(row.get("idempotent_hit"))),
                utc_now(), utc_now(),
            ),
        )

    def get_restore_request(self, request_no: str) -> dict[str, Any] | None:
        return self.engine.query_one(
            "SELECT * FROM restore_requests WHERE request_no=?", (request_no,))

    def find_by_idempotency_key(self, key: str | None) -> dict[str, Any] | None:
        if not key:
            return None
        return self.engine.query_one(
            "SELECT * FROM restore_requests WHERE idempotency_key=?", (key,))

    def find_open_by_archive(self, archive_no: str) -> dict[str, Any] | None:
        rows = self.engine.query_all(
            "SELECT * FROM restore_requests WHERE archive_no=? AND request_status != '已办结' "
            "ORDER BY created_at",
            (archive_no,),
        )
        return rows[0] if rows else None

    def list_restore_requests(self, archive_no: str | None = None) -> list[dict[str, Any]]:
        if archive_no:
            return self.engine.query_all(
                "SELECT * FROM restore_requests WHERE archive_no=? ORDER BY created_at", (archive_no,))
        return self.engine.query_all("SELECT * FROM restore_requests ORDER BY created_at")

    def update_restore_request(self, request_no: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = utc_now()
        cols = ", ".join(f"{k}=?" for k in fields)
        params = tuple(fields.values()) + (request_no,)
        self.engine.execute(
            f"UPDATE restore_requests SET {cols} WHERE request_no=?", params)

    def add_restore_event(
        self, *, request_no: str, event_type: str, from_stage: str | None, to_stage: str | None,
        data_source: str | None, note: str,
    ) -> None:
        self.engine.execute(
            """
            INSERT INTO restore_events(request_no, event_type, from_stage, to_stage, data_source, note, recorded_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (request_no, event_type, from_stage, to_stage, data_source, note, utc_now()),
        )

    def list_restore_events(self, request_no: str) -> list[dict[str, Any]]:
        return self.engine.query_all(
            "SELECT * FROM restore_events WHERE request_no=? ORDER BY id", (request_no,))

    # ---------------- 切片恢复 ----------------

    def insert_slice_recovery(self, row: dict[str, Any]) -> int:
        return self.engine.execute(
            """
            INSERT INTO slice_recoveries(
                request_no, slice_batch, archive_no, version, exp_code, storage_version,
                sample_version, data_source, compute_input_json, metric_value, evidence_json,
                recover_status, created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row["request_no"], row["slice_batch"], row["archive_no"], row["version"],
                row.get("exp_code"), row["storage_version"], row["sample_version"],
                row["data_source"], json.dumps(row["compute_input"], ensure_ascii=False),
                row.get("metric_value"), json.dumps(row["evidence"], ensure_ascii=False),
                row["recover_status"], utc_now(),
            ),
        )

    def get_slice_by_request(self, request_no: str) -> dict[str, Any] | None:
        return self.engine.query_one(
            "SELECT * FROM slice_recoveries WHERE request_no=?", (request_no,))

    def get_slice_by_batch(self, slice_batch: str) -> dict[str, Any] | None:
        return self.engine.query_one(
            "SELECT * FROM slice_recoveries WHERE slice_batch=?", (slice_batch,))

    def list_slice_recoveries(self, archive_no: str | None = None) -> list[dict[str, Any]]:
        if archive_no:
            return self.engine.query_all(
                "SELECT * FROM slice_recoveries WHERE archive_no=? ORDER BY id", (archive_no,))
        return self.engine.query_all("SELECT * FROM slice_recoveries ORDER BY id")

    # ---------------- 部署/回滚流水 ----------------

    def add_deployment(self, *, deployment_no: str, action: str, result: str, note: str) -> int:
        return self.engine.execute(
            "INSERT INTO deployment_journal(deployment_no, action, result, note, recorded_at) "
            "VALUES(?,?,?,?,?)",
            (deployment_no, action, result, note, utc_now()),
        )

    def list_deployments(self) -> list[dict[str, Any]]:
        return self.engine.query_all("SELECT * FROM deployment_journal ORDER BY id")

    def latest_deployment(self) -> dict[str, Any] | None:
        rows = self.engine.query_all("SELECT * FROM deployment_journal ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None
