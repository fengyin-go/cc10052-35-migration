"""归档迁移恢复的 HTTP 接口集成测试（标准库实现，不引入 httpx/pytest）。

在后台线程里启动真实 uvicorn（FastAPI 路由、CORS、启动钩子全链路），
用 urllib 调用，验证：
- 场景引导、按旧编号查进度、整包迁移、恢复受理/续办、切片证据、部署流水；
- 切片分析器通用接口合并恢复切片，可复核原归档号与样本版次；
- 已办结恢复重复触发不产生第二条切片（接口级幂等）。

运行：cd backend && ARCHIVE_AUTO_RECOVERY=0 .venv/bin/python -m unittest app.tests.test_archive_http -v
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import uvicorn

from app.archive.engine import reset_engine


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _ServerThread(threading.Thread):
    def __init__(self, port: int, db_path: str) -> None:
        super().__init__(daemon=True)
        os.environ["ARCHIVE_DB_PATH"] = db_path
        # 关闭启动自动续办，保证测试按既定顺序显式驱动各阶段。
        os.environ["ARCHIVE_AUTO_RECOVERY"] = "0"
        self.config = uvicorn.Config(
            "app.main:app", host="127.0.0.1", port=port,
            log_level="warning", lifespan="on")
        self.server = uvicorn.Server(self.config)

    def run(self) -> None:
        self.server.run()

    def stop(self) -> None:
        self.server.should_exit = True


class ArchiveHttpTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp()
        cls.db_path = os.path.join(cls.tmp, "http.sqlite3")
        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = _ServerThread(cls.port, cls.db_path)
        cls.thread.start()
        cls._wait_ready()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.thread.stop()
        reset_engine()

    @classmethod
    def _wait_ready(cls) -> None:
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"{cls.base}/api/health", timeout=1) as resp:
                    if resp.status == 200:
                        return
            except (urllib.error.URLError, ConnectionError, OSError):
                time.sleep(0.25)
        raise RuntimeError("测试服务未在限定时间内就绪")

    def _call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"{self.base}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_full_a17_flow_over_http(self) -> None:
        # 0. 引导现场
        status, boot = self._call("POST", "/api/archive-migration/scenario/bootstrap")
        self.assertEqual(status, 200)
        request_no = boot["openRequestNo"]
        self.assertTrue(request_no)

        # 1. 老编号查进度：v2 停在摘要、缺失材料可查
        status, progress = self._call("GET", "/api/archive-migration/packages/A17/progress")
        self.assertEqual(status, 200)
        v2 = next(v for v in progress["versions"] if v["version"] == "v2")
        self.assertEqual(v2["packageStatus"], "进行中")
        self.assertIn("summary.保留等级", v2["missingFields"])

        # 2. 未办结请求可查所处阶段
        status, detail = self._call("GET", f"/api/archive-migration/restores/{request_no}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["stage"], "已接收")

        # 3. 整包迁移 v2 与 v1（不合并）
        status, mig = self._call("POST", "/api/archive-migration/packages/migrate",
                                 {"archiveNo": "A17", "version": "v2"})
        self.assertEqual(status, 200)
        self.assertEqual(mig["package"]["packageStatus"], "已确认全包")
        self.assertTrue(mig["credential"])
        status, mig_v1 = self._call("POST", "/api/archive-migration/packages/migrate",
                                    {"archiveNo": "A17", "version": "v1"})
        self.assertEqual(status, 200)
        self.assertEqual(mig_v1["package"]["version"], "v1")

        # 4. 续办原请求 -> 办结，数据源为 v2 SQLite
        status, cont = self._call("POST", "/api/archive-migration/restores/continue",
                                  {"requestNo": request_no})
        self.assertEqual(status, 200)
        self.assertEqual(cont["request"]["stage"], "已办结")
        self.assertIn("SQLite", cont["request"]["dataSource"])
        slice_batch = cont["request"]["sliceBatch"]

        # 5. 接口级幂等：再续办/再受理都不产生第二条切片
        status, again = self._call("POST", "/api/archive-migration/restores/continue",
                                   {"requestNo": request_no})
        self.assertTrue(again.get("idempotent"))
        status, resubmit = self._call("POST", "/api/archive-migration/restores",
                                      {"archiveNo": "A17", "idempotencyKey": "http-k"})
        self.assertTrue(resubmit.get("idempotent"))
        status, slices = self._call("GET", "/api/archive-migration/slices?archiveNo=A17")
        self.assertEqual(slices["total"], 1)

        # 6. 切片明细核对原归档号、样本版次、证据
        status, slc = self._call("GET", f"/api/archive-migration/slices/{slice_batch}")
        self.assertEqual(status, 200)
        self.assertEqual(slc["archiveNo"], "A17")
        self.assertEqual(slc["version"], "v2")
        self.assertEqual(slc["sampleVersion"], "v2-sample-20261012")
        self.assertEqual(slc["storageVersion"], "v2")
        self.assertEqual(slc["metricValue"], 0.027)
        self.assertIn("A17 留存实验 v2", slc["evidence"]["summarySnapshot"])

        # 7. 切片分析器通用接口合并恢复切片，可复核
        status, segment = self._call("GET", "/api/segment_slice?keyword=SG-RC")
        self.assertEqual(status, 200)
        merged = [r for r in segment["items"] if r["切片批次"] == slice_batch]
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["原归档号"], "A17")
        self.assertEqual(merged[0]["样本版次"], "v2-sample-20261012")

        # 8. 回滚后仍可说明结果
        status, _ = self._call("POST", "/api/archive-migration/deployments",
                               {"action": "回滚", "note": "HTTP 演练"})
        self.assertEqual(status, 200)
        status, after = self._call("GET", f"/api/archive-migration/restores/{request_no}")
        self.assertEqual(after["stage"], "已办结")
        self.assertTrue(after["dataSource"])
        event_types = [e["eventType"] for e in after["events"]]
        self.assertEqual(event_types, ["受理", "凭证校验", "取数", "切片恢复", "办结"])

        # 9. 未知归档受理返回 404
        status, nf = self._call("POST", "/api/archive-migration/restores", {"archiveNo": "NOPE"})
        self.assertEqual(status, 404)
        self.assertIn("detail", nf)


if __name__ == "__main__":
    unittest.main(verbosity=2)
