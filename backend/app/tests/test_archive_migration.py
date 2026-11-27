"""归档迁移与恢复的可重复测试（仅用标准库 unittest，不引入新依赖）。

覆盖题目全部硬性约束：
- 进程内存归档整包迁移到 SQLite，按原归档号查进度与缺失材料；
- 迁移恢复凭证签发/校验，切片分析器据原样本取数；
- 同实验编码不同归档版次不合并；摘要、指标、恢复依据整包迁移；缺字段可查；
- 原接口按原编号受理，已受理可从原请求继续，已办结绝不执行两次；
- 切片按归档存储 v2 取实际计算输入，明细可核对原归档号与样本版次、证据；
- 重启/回滚后处理结果仍可说明；全新进程仅凭 SQLite 也能续办（不依赖内存旧库）。

运行：cd backend && ARCHIVE_AUTO_RECOVERY=0 .venv/bin/python -m unittest app.tests.test_archive_migration -v
"""
from __future__ import annotations

import os
import tempfile
import unittest

from app.archive import models
from app.archive.credential import verify_credential
from app.archive.deployment import DeploymentService
from app.archive.engine import init_engine, reset_engine
from app.archive.migration import MigrationService
from app.archive.repository import ArchiveRepository
from app.archive.restore import RestoreService
from app.archive.scenario import ARCHIVE_A17, EXP_CODE_A17, bootstrap_a17_scenario


class ArchiveMigrationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.sqlite3")
        init_engine(self.db_path)
        bootstrap_a17_scenario(self.db_path)
        self.repo = ArchiveRepository()
        self.migration = MigrationService(self.repo)
        self.restore = RestoreService(self.repo)
        self.deployment = DeploymentService(self.repo, self.restore)
        self.request_no = self.repo.find_open_by_archive(ARCHIVE_A17)["request_no"]

    def tearDown(self) -> None:
        reset_engine()

    # ---- 迁移进度与缺失材料 ----

    def test_progress_shows_summary_only_and_missing_fields(self) -> None:
        progress = self.migration.progress(ARCHIVE_A17)
        v2 = next(v for v in progress["versions"] if v["version"] == "v2")
        self.assertEqual(v2["stage"], models.STAGE_SUMMARY)
        self.assertEqual(v2["packageStatus"], models.PACKAGE_IN_PROGRESS)
        self.assertIn("summary.保留等级", v2["missingFields"])
        v1 = next(v for v in progress["versions"] if v["version"] == "v1")
        self.assertEqual(v1["packageStatus"], "未开始")

    def test_full_package_migration_confirms_and_records_missing(self) -> None:
        result = self.migration.migrate_archive(ARCHIVE_A17, "v2")
        self.assertTrue(result["ok"])
        self.assertEqual(result["package"]["packageStatus"], models.PACKAGE_CONFIRMED)
        self.assertIn("summary.保留等级", result["missingFields"])
        # 整包三段都有流水
        journal = self.repo.list_journal(ARCHIVE_A17, "v2")
        self.assertEqual({j["stage"] for j in journal}, set(models.MIGRATION_STAGES))
        # 恢复依据完整迁移
        self.assertEqual(len(self.repo.list_recovery_basis(ARCHIVE_A17, "v2")), 2)

    def test_migration_is_idempotent(self) -> None:
        self.migration.migrate_archive(ARCHIVE_A17, "v2")
        again = self.migration.migrate_archive(ARCHIVE_A17, "v2")
        self.assertTrue(again["ok"])
        self.assertIn("已确认全包", again["message"])

    def test_versions_with_same_exp_code_are_not_merged(self) -> None:
        self.migration.migrate_archive(ARCHIVE_A17, "v2")
        self.migration.migrate_archive(ARCHIVE_A17, "v1")
        pkg_v2 = self.repo.get_package(ARCHIVE_A17, "v2")
        pkg_v1 = self.repo.get_package(ARCHIVE_A17, "v1")
        self.assertEqual(pkg_v2["exp_code"], pkg_v1["exp_code"])
        self.assertEqual(pkg_v2["exp_code"], EXP_CODE_A17)
        self.assertNotEqual(pkg_v2["version"], pkg_v1["version"])
        self.assertEqual(len(self.migration.progress(ARCHIVE_A17)["versions"]), 2)

    def test_missing_source_fields_are_queryable_for_v1(self) -> None:
        result = self.migration.migrate_archive(ARCHIVE_A17, "v1")
        joined = " ".join(result["missingFields"])
        self.assertIn("样本量核对", joined)
        self.assertIn("置信区间", joined)

    # ---- 凭证 ----

    def test_credential_bound_to_archive_no(self) -> None:
        self.migration.migrate_archive(ARCHIVE_A17, "v2")
        credential = self.repo.get_credential(ARCHIVE_A17)["credential"]
        self.assertTrue(verify_credential(credential, archive_no=ARCHIVE_A17)["valid"])
        wrong = verify_credential(credential, archive_no="OTHER")
        self.assertFalse(wrong["valid"])
        self.assertIn("原归档号", wrong["reason"])

    # ---- 恢复受理/续办/幂等 ----

    def test_continue_unfinished_request_using_v2_source(self) -> None:
        self.migration.migrate_archive(ARCHIVE_A17, "v2")
        out = self.restore.continue_restore(self.request_no)
        self.assertTrue(out["ok"])
        req = out["request"]
        self.assertEqual(req["stage"], models.RESTORE_STAGE_DONE)
        self.assertEqual(req["requestStatus"], models.RESTORE_STATUS_DONE)
        self.assertIn("v2", req["dataSource"])
        self.assertEqual(req["sampleVersion"], "v2-sample-20261012")

    def test_restore_runs_slice_exactly_once(self) -> None:
        self.migration.migrate_archive(ARCHIVE_A17, "v2")
        self.restore.continue_restore(self.request_no)
        again = self.restore.continue_restore(self.request_no)
        self.assertTrue(again.get("idempotent"))
        submit = self.restore.submit_restore(archive_no=ARCHIVE_A17, idempotency_key="k2")
        self.assertTrue(submit.get("idempotent"))
        self.assertEqual(len(self.repo.list_slice_recoveries(ARCHIVE_A17)), 1)

    def test_open_request_is_resumed_instead_of_duplicated(self) -> None:
        out = self.restore.submit_restore(archive_no=ARCHIVE_A17, idempotency_key="another")
        self.assertTrue(out.get("idempotent"))
        self.assertEqual(out["request"]["requestNo"], self.request_no)

    # ---- 切片证据与复核 ----

    def test_slice_detail_cross_checks_archive_no_sample_version_and_summary(self) -> None:
        self.migration.migrate_archive(ARCHIVE_A17, "v2")
        self.restore.continue_restore(self.request_no)
        detail = self.restore.get_request(self.request_no)
        slc = detail["slice"]
        self.assertEqual(slc["archiveNo"], ARCHIVE_A17)
        self.assertEqual(slc["version"], "v2")
        self.assertEqual(slc["sampleVersion"], "v2-sample-20261012")
        self.assertEqual(slc["storageVersion"], "v2")
        self.assertEqual(slc["metricValue"], 0.027)
        self.assertTrue(slc["evidence"]["inputHash"])
        self.assertIn("A17 留存实验 v2", slc["evidence"]["summarySnapshot"])
        self.assertEqual(slc["computeInput"]["treatMean"], 0.151)

    # ---- 回滚后结果可说明 ----

    def test_rollback_keeps_done_result_and_journal(self) -> None:
        self.migration.migrate_archive(ARCHIVE_A17, "v2")
        self.restore.continue_restore(self.request_no)
        self.deployment.record(action="回滚", note="演练")
        detail = self.restore.get_request(self.request_no)
        self.assertEqual(detail["stage"], models.RESTORE_STAGE_DONE)
        self.assertTrue(detail["dataSource"])
        actions = [d["action"] for d in self.deployment.journal()]
        self.assertIn("回滚", actions)

    # ---- 全新进程仅凭 SQLite 续办（不依赖内存旧库） ----

    def test_fresh_process_recovers_from_sqlite_only(self) -> None:
        # 先完成整包迁移，让源快照落库，但不办理恢复。
        self.migration.migrate_archive(ARCHIVE_A17, "v2")
        # 模拟重启：重建引擎（同库文件），并用空内存仓库替代旧库依赖。
        reset_engine()
        init_engine(self.db_path)
        repo2 = ArchiveRepository()
        restore2 = RestoreService(repo2, legacy=_EmptyLegacySource())
        out = restore2.continue_restore(self.request_no)
        self.assertTrue(out["ok"])
        self.assertEqual(out["request"]["stage"], models.RESTORE_STAGE_DONE)
        self.assertIn("SQLite", out["request"]["dataSource"])
        self.assertEqual(len(repo2.list_slice_recoveries(ARCHIVE_A17)), 1)

    def test_blocked_source_keeps_request_resumable(self) -> None:
        # 未迁移 + 无内存源：取数受阻但保持未办结，之后补迁移可续。
        reset_engine()
        init_engine(self.db_path)
        repo2 = ArchiveRepository()
        restore2 = RestoreService(repo2, legacy=_EmptyLegacySource())
        out = restore2.continue_restore(self.request_no)
        self.assertFalse(out["ok"])
        self.assertEqual(repo2.find_open_by_archive(ARCHIVE_A17)["stage"],
                         models.RESTORE_STAGE_CREDENTIAL)
        # 补回内存源并完成迁移后续办成功
        restore_with_legacy = RestoreService(repo2)
        MigrationService(repo2).migrate_archive(ARCHIVE_A17, "v2")
        done = restore_with_legacy.continue_restore(self.request_no)
        self.assertTrue(done["ok"])
        self.assertEqual(done["request"]["stage"], models.RESTORE_STAGE_DONE)


class _EmptyLegacySource:
    """模拟全新进程：进程内存旧库没有任何归档行。"""

    def get_archive(self, archive_no: str, version: str | None = None):  # noqa: ANN201
        return None

    def get_archive_versions(self, archive_no: str):  # noqa: ANN201
        return []

    def read_sample(self, row):  # noqa: ANN201
        return {}

    def read_sample_version(self, row):  # noqa: ANN201
        return "unknown"


if __name__ == "__main__":
    unittest.main(verbosity=2)
