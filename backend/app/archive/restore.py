"""恢复服务：原接口按原编号继续领取恢复，切片只成功办理一次，重启/回滚可续办且可复核。

核心保证：
- 恢复请求按原归档号受理；幂等键或"该归档未办结请求"保证同一恢复不会被执行两次。
- 已办结请求再次提交直接返回原结果（幂等命中），不会再产生新切片。
- 已接收未办结的请求持久化阶段与实际数据源，重启/回滚后可从原请求继续。
- 切片恢复优先取归档存储版本 v2 的实际计算输入；v2 不可用时回退兼容旧库 v1，
  并把真实数据源、样本版次、证据全部落库，页面可复核归档版次与证据。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.archive import models
from app.archive.credential import verify_credential
from app.archive.legacy import LegacyArchiveSource, read_sample, read_sample_version
from app.archive.repository import ArchiveRepository


class RestoreService:
    def __init__(
        self,
        repository: ArchiveRepository | None = None,
        legacy: LegacyArchiveSource | None = None,
    ) -> None:
        self.repo = repository or ArchiveRepository()
        self.legacy = legacy or LegacyArchiveSource()

    # ---------------- 受理 ----------------

    def submit_restore(
        self, *, archive_no: str, version: str | None = None,
        idempotency_key: str | None = None, credential: str | None = None,
    ) -> dict[str, Any]:
        # 1) 幂等键：同一键直接回到原请求，绝不重复办理。
        if idempotency_key:
            existed = self.repo.find_by_idempotency_key(idempotency_key)
            if existed is not None:
                return self._idempotent_return(existed, "命中幂等键，返回已受理/已办结请求")

        # 2) 同一归档存在未办结请求：从原请求继续，不新建。
        open_request = self.repo.find_open_by_archive(archive_no)
        if open_request is not None:
            return self._idempotent_return(open_request, "该归档已有未办结恢复，从原请求继续")

        # 3) 已办结请求：恢复只允许成功一次，直接返回原结果。
        done = self._find_done_by_archive(archive_no)
        if done is not None:
            return self._idempotent_return(done, "该归档恢复已办结，禁止重复执行")

        target_version, legacy_row = self._resolve_version(archive_no, version)
        if legacy_row is None:
            return {"ok": False, "message": f"旧库找不到原归档号 {archive_no} 的归档包"}

        request_no = self._new_request_no(archive_no)
        stored_credential = self.repo.get_credential(archive_no)
        effective_credential = credential or (
            stored_credential.get("credential") if stored_credential else None)
        credential_valid = False
        if effective_credential:
            check = verify_credential(effective_credential, archive_no=archive_no)
            credential_valid = bool(check.get("valid"))

        row = {
            "request_no": request_no,
            "archive_no": archive_no,
            "version": target_version,
            "idempotency_key": idempotency_key,
            "stage": models.RESTORE_STAGE_RECEIVED,
            "request_status": models.RESTORE_STATUS_OPEN,
            "credential": effective_credential,
            "credential_valid": credential_valid,
            "data_source": None,
            "storage_version": None,
            "sample_version": None,
            "slice_batch": None,
            "result_summary": None,
            "idempotent_hit": False,
        }
        self.repo.insert_restore_request(row)
        self.repo.add_restore_event(
            request_no=request_no, event_type="受理", from_stage=None,
            to_stage=models.RESTORE_STAGE_RECEIVED, data_source=None,
            note="原接口按原编号受理恢复请求")
        return {"ok": True, "message": "恢复请求已受理，等待继续办理",
                "request": models.serialize_restore(self.repo.get_restore_request(request_no))}  # type: ignore[arg-type]

    # ---------------- 继续办理（可重启/回滚后续跑） ----------------

    def continue_restore(self, request_no: str) -> dict[str, Any]:
        req = self.repo.get_restore_request(request_no)
        if req is None:
            return {"ok": False, "message": f"恢复请求 {request_no} 不存在"}
        if req["request_status"] == models.RESTORE_STAGE_DONE or req["stage"] == models.RESTORE_STAGE_DONE:
            # 已办结：不执行第二次，直接回原结果。
            return self._idempotent_return(req, "请求已办结，跳过重复执行")

        with self.repo.engine.write_lock:
            # 阶段一：凭证校验（已校验则跳过）。
            if req["stage"] == models.RESTORE_STAGE_RECEIVED:
                valid, reason = self._check_credential(req)
                self.repo.update_restore_request(
                    request_no, stage=models.RESTORE_STAGE_CREDENTIAL, credential_valid=valid)
                self.repo.add_restore_event(
                    request_no=request_no, event_type="凭证校验",
                    from_stage=models.RESTORE_STAGE_RECEIVED,
                    to_stage=models.RESTORE_STAGE_CREDENTIAL, data_source=None,
                    note=reason)
                req = self.repo.get_restore_request(request_no)  # type: ignore[assignment]

            # 阶段二：确定实际数据源（v2 优先，缺失则兼容旧库 v1）。
            if req["stage"] == models.RESTORE_STAGE_CREDENTIAL:
                source_info = self._resolve_data_source(req)
                if not source_info.get("available", True):
                    # 源不可用：停在凭证阶段之后、取数之前，保留未办结现场，迁移完成后可续。
                    self.repo.add_restore_event(
                        request_no=request_no, event_type="取数受阻",
                        from_stage=models.RESTORE_STAGE_CREDENTIAL,
                        to_stage=models.RESTORE_STAGE_CREDENTIAL,
                        data_source=source_info["data_source"], note=source_info["note"])
                    return {"ok": False, "message": source_info["note"],
                            "request": models.serialize_restore(
                                self.repo.get_restore_request(request_no))}
                self.repo.update_restore_request(
                    request_no, stage=models.RESTORE_STAGE_FETCH,
                    data_source=source_info["data_source"],
                    storage_version=source_info["storage_version"],
                    sample_version=source_info["sample_version"])
                self.repo.add_restore_event(
                    request_no=request_no, event_type="取数",
                    from_stage=models.RESTORE_STAGE_CREDENTIAL,
                    to_stage=models.RESTORE_STAGE_FETCH,
                    data_source=source_info["data_source"],
                    note=source_info["note"])
                req = self.repo.get_restore_request(request_no)  # type: ignore[assignment]

            # 阶段三：切片恢复（幂等：已有切片则不重复计算）。
            if req["stage"] == models.RESTORE_STAGE_FETCH:
                slice_result = self._recover_slice(req)
                if not slice_result.get("ok"):
                    return {"ok": False, "message": slice_result["message"],
                            "request": models.serialize_restore(req)}
                self.repo.update_restore_request(
                    request_no, stage=models.RESTORE_STAGE_SLICE,
                    slice_batch=slice_result["slice_batch"])
                self.repo.add_restore_event(
                    request_no=request_no, event_type="切片恢复",
                    from_stage=models.RESTORE_STAGE_FETCH,
                    to_stage=models.RESTORE_STAGE_SLICE,
                    data_source=req["data_source"],
                    note=f"切片批次 {slice_result['slice_batch']} 已恢复")
                req = self.repo.get_restore_request(request_no)  # type: ignore[assignment]

            # 阶段四：办结。
            if req["stage"] == models.RESTORE_STAGE_SLICE:
                slice_row = self.repo.get_slice_by_request(request_no)
                summary = (
                    f"恢复办结：采用 {req['storage_version']} 样本版次 {req['sample_version']}，"
                    f"数据源 {req['data_source']}，切片 {req['slice_batch']}"
                )
                self.repo.update_restore_request(
                    request_no, stage=models.RESTORE_STAGE_DONE,
                    request_status=models.RESTORE_STATUS_DONE, result_summary=summary)
                self.repo.add_restore_event(
                    request_no=request_no, event_type="办结",
                    from_stage=models.RESTORE_STAGE_SLICE,
                    to_stage=models.RESTORE_STAGE_DONE, data_source=req["data_source"],
                    note=summary)
                req = self.repo.get_restore_request(request_no)  # type: ignore[assignment]

        return {"ok": True, "message": "恢复已办结",
                "request": models.serialize_restore(self.repo.get_restore_request(request_no))}  # type: ignore[arg-type]

    # ---------------- 查询 ----------------

    def get_request(self, request_no: str) -> dict[str, Any] | None:
        req = self.repo.get_restore_request(request_no)
        if req is None:
            return None
        result = models.serialize_restore(req)
        result["events"] = [
            {"eventType": e["event_type"], "fromStage": e["from_stage"],
             "toStage": e["to_stage"], "dataSource": e["data_source"],
             "note": e["note"], "recordedAt": e["recorded_at"]}
            for e in self.repo.list_restore_events(request_no)
        ]
        slice_row = self.repo.get_slice_by_request(request_no)
        result["slice"] = models.serialize_slice(slice_row) if slice_row else None
        return result

    def list_requests(self, archive_no: str | None = None) -> list[dict[str, Any]]:
        return [models.serialize_restore(r) for r in self.repo.list_restore_requests(archive_no)]

    def list_unfinished(self) -> list[dict[str, Any]]:
        return [
            models.serialize_restore(r)
            for r in self.repo.list_restore_requests()
            if r["request_status"] != models.RESTORE_STATUS_DONE
        ]

    def get_slice_detail(self, slice_batch: str) -> dict[str, Any] | None:
        row = self.repo.get_slice_by_batch(slice_batch)
        return models.serialize_slice(row) if row else None

    # ---------------- 内部方法 ----------------

    def _resolve_version(self, archive_no: str, version: str | None) -> tuple[str, dict[str, Any] | None]:
        # 优先用迁移库中已持久化的整包源数据（进程重启后仍在），其次回退内存旧库。
        package = self.repo.get_package_by_archive_no(archive_no) if version is None \
            else self.repo.get_package(archive_no, version)
        if package is not None:
            source = self._source_from_package(package)
            if source is not None:
                return str(package["version"]), source
        legacy_row = self.legacy.get_archive(archive_no, version)
        if legacy_row is None:
            return "", None
        return str(legacy_row.get("归档版次", "")), legacy_row

    def _source_from_package(self, package: dict[str, Any]) -> dict[str, Any] | None:
        """从迁移包持久化的源快照还原一个与旧库同构的行（重启后恢复用）。

        摘要阶段尚未持久化完整源（source_payload 为空），返回 None 以便回退旧库。
        """
        raw = package.get("source_payload_json")
        if not raw:
            return None
        try:
            import json
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) and parsed else None

    def _load_source_row(self, archive_no: str, version: str) -> dict[str, Any] | None:
        """恢复取数统一入口：迁移库持久化源优先，内存旧库兜底。"""
        package = self.repo.get_package(archive_no, version)
        if package is not None:
            source = self._source_from_package(package)
            if source is not None:
                return source
        return self.legacy.get_archive(archive_no, version)

    def _check_credential(self, req: dict[str, Any]) -> tuple[bool, str]:
        credential = req.get("credential")
        if not credential:
            return False, "未携带迁移恢复凭证，按无凭证继续（取数阶段标注来源）"
        check = verify_credential(credential, archive_no=req["archive_no"])
        if check.get("valid"):
            return True, "迁移恢复凭证校验通过"
        return False, f"凭证校验未通过：{check.get('reason')}"

    def _resolve_data_source(self, req: dict[str, Any]) -> dict[str, Any]:
        archive_no, version = req["archive_no"], req["version"]
        package = self.repo.get_package(archive_no, version)
        source_row = self._load_source_row(archive_no, version)
        if source_row is None:
            # 无任何可用源：显式标注，请求停在取数前保持未办结，等整包迁移后可续。
            return {
                "data_source": "无可用源（迁移未完成且内存旧库已失效）",
                "storage_version": None,
                "sample_version": None,
                "note": "等待整包迁移完成后再继续，请求保持未办结",
                "available": False,
            }
        sample = read_sample(source_row)
        sample_version = read_sample_version(source_row)
        from_persisted = bool(package is not None and self._source_from_package(package) is not None)

        # v2 优先：迁移包已确认、源已持久化且带 v2 样本输入。
        v2_ready = (
            package is not None
            and package.get("package_status") == models.PACKAGE_CONFIRMED
            and str(package.get("storage_version")) == models.STORAGE_V2
            and bool(sample)
        )
        if v2_ready:
            return {
                "data_source": models.SOURCE_SQLITE_V2,
                "storage_version": models.STORAGE_V2,
                "sample_version": sample_version,
                "note": "迁移包已确认，按归档存储版本 v2 取实际计算输入"
                        + ("" if from_persisted else "（来自内存旧库）"),
                "available": True,
            }
        # 回退：兼容旧库 v1（迁移未完成/摘要阶段），仍可恢复但显式标注真实来源。
        return {
            "data_source": models.SOURCE_LEGACY_V1,
            "storage_version": str(source_row.get("归档存储版本", models.STORAGE_V1)),
            "sample_version": sample_version,
            "note": "v2 整包尚未确认，回退兼容旧库 v1 取数，来源已落库可查",
            "available": True,
        }

    def _recover_slice(self, req: dict[str, Any]) -> dict[str, Any]:
        request_no = req["request_no"]
        # 幂等：该请求已产生切片则不重复计算（上线/回滚重放安全）。
        existing = self.repo.get_slice_by_request(request_no)
        if existing is not None:
            return {"ok": True, "slice_batch": existing["slice_batch"], "idempotent": True}

        archive_no, version = req["archive_no"], req["version"]
        source_row = self._load_source_row(archive_no, version)
        if source_row is None:
            return {"ok": False, "message": "迁移库与旧库均无该归档源数据，无法取切片输入"}
        compute_input = read_sample(source_row)
        if not compute_input:
            return {"ok": False, "message": "无可用样本输入，切片恢复失败"}

        metric_value = self._compute(compute_input)
        slice_batch = req.get("slice_batch") or self._new_slice_batch(archive_no, request_no)
        evidence = {
            "archiveNo": archive_no,
            "version": version,
            "expCode": source_row.get("实验编码", ""),
            "storageVersion": req["storage_version"],
            "sampleVersion": req["sample_version"],
            "dataSource": req["data_source"],
            "inputHash": models.stable_hash(compute_input),
            "summarySnapshot": source_row.get("结论摘要", ""),
            "recoveredAt": datetime.now(timezone.utc).isoformat(),
            "formula": "metric_value = (treat_mean - ctrl_mean) * effect_weight",
        }
        self.repo.insert_slice_recovery({
            "request_no": request_no,
            "slice_batch": slice_batch,
            "archive_no": archive_no,
            "version": version,
            "exp_code": source_row.get("实验编码", ""),
            "storage_version": req["storage_version"],
            "sample_version": req["sample_version"],
            "data_source": req["data_source"],
            "compute_input": compute_input,
            "metric_value": metric_value,
            "evidence": evidence,
            "recover_status": "已恢复",
        })
        return {"ok": True, "slice_batch": slice_batch, "idempotent": False}

    @staticmethod
    def _compute(compute_input: dict[str, Any]) -> float:
        ctrl = float(compute_input.get("ctrlMean", 0) or 0)
        treat = float(compute_input.get("treatMean", 0) or 0)
        weight = float(compute_input.get("effectWeight", 1) or 1)
        return round((treat - ctrl) * weight, 6)

    def _find_done_by_archive(self, archive_no: str) -> dict[str, Any] | None:
        rows = self.repo.list_restore_requests(archive_no)
        done = [r for r in rows if r["request_status"] == models.RESTORE_STATUS_DONE]
        return done[0] if done else None

    def _idempotent_return(self, row: dict[str, Any], message: str) -> dict[str, Any]:
        return {"ok": True, "message": message, "idempotent": True,
                "request": models.serialize_restore(row)}

    @staticmethod
    def _new_request_no(archive_no: str) -> str:
        return f"RR-{archive_no}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"

    @staticmethod
    def _new_slice_batch(archive_no: str, request_no: str) -> str:
        suffix = datetime.now(timezone.utc).strftime("%H%M%S%f")[:10]
        return f"SG-RC-{archive_no}-{suffix}"
