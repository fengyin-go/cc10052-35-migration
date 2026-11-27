# 实验指标分析与归因评估平台

支撑AB实验分组、指标口径计算、显著性检验、归因分析与报告分发的实验评估平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8022/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5202/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8022`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 事件接入 | `event_stream` | 事件定义 | 事件编码、事件名称、属性字段 |
| 实验设计 | `exp_design` | 实验方案 | 实验编码、实验名称、分组策略 |
| 分组分配器 | `variant_assign` | 分组结果 | 分配批次、用户标识、实验编码 |
| 指标口径中心 | `metric_catalog` | 指标定义 | 指标编码、指标名称、计算口径 |
| 指标计算作业 | `metric_compute` | 计算作业 | 作业名称、指标编码、计算窗口 |
| 显著性检验 | `significance_test` | 检验结果 | 检验批次、实验编码、指标编码 |
| 归因分析器 | `attr_analysis` | 归因结果 | 归因批次、实验编码、贡献因子 |
| 人群圈选器 | `cohort_builder` | 人群包 | 人群编码、人群名称、圈选条件 |
| 报告生成器 | `report_gen` | 分析报告 | 报告编码、实验编码、报告模板 |
| 报告分发 | `report_dist` | 分发记录 | 分发批次、报告编码、接收方 |
| 窗口调度器 | `window_scheduler` | 调度计划 | 计划名称、指标编码、窗口粒度 |
| 数据时效监控 | `data_freshness` | 时效记录 | 监控对象、数据域、最新水位 |
| 指标异常检测 | `anomaly_metric` | 异常事件 | 异常编号、指标编码、基线区间 |
| 实验归档库 | `exp_archive` | 归档实验 | 实验编码、结束时间、结论摘要 |
| 样本量核对 | `sample_ratio` | 核对结果 | 核对批次、实验编码、计划比例 |
| AA预检器 | `aa_check` | AA检验 | 检验编码、实验编码、检验窗口 |
| 切片分析器 | `segment_slice` | 切片结果 | 切片批次、实验编码、切片维度 |
| 曝光日志接入 | `exposure_log` | 曝光记录 | 日志批次、实验编码、用户标识 |
| 结论评审队列 | `conclusion_review` | 评审记录 | 记录编号、实验编码、评审结论 |
| 指标查询服务 | `metric_api` | 查询接口 | 接口编码、指标编码、服务对象 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 实验归档迁移与恢复（SQLite 持久化）

把实验归档库的进程内归档整包迁移到 SQLite，支撑上线切换/回滚/重启后的可续办与可审计。

- 后端代码：`backend/app/archive/`
  - `db.py` / `engine.py`：SQLite 建表（WAL）、连接与写锁、轻量列迁移。
  - `repository.py`：集中所有 SQL；`migration.py` 按原归档号整包迁移（摘要/指标/恢复依据三段）。
  - `credential.py`：HMAC 迁移恢复凭证（绑定原归档号，可离线核验）。
  - `restore.py`：原编号受理、幂等续办、v2 优先取数（兼容旧库回退）、切片只成功一次。
  - `deployment.py`：上线/回滚流水与启动时未办结恢复续办。
  - `scenario.py`：A17 演练现场（摘要已写未确认、未办结恢复、一次回滚）。
- 接口前缀：`/api/archive-migration`（进度、整包迁移、恢复受理/续办、切片证据、部署流水）。
- 前端页面：导航「归档迁移与恢复」→ `/archive-migration`（`ArchiveMigrationView.vue`）。
- 切片分析器 `segment_slice` 列表会合并迁移库中已恢复切片，可在原页面核对原归档号与样本版次。

关键约束的落地点：

- 同一实验编码的不同归档版次以 `(archive_no, version)` 唯一存放，绝不合并。
- 摘要、指标、已登记恢复依据作为完整一份数据迁移；缺失源字段登记到 `missingFields` 可查。
- 整包源快照随迁移落库，全新进程仅凭 SQLite 即可续办，不依赖进程内存旧库。
- 已办结恢复重复触发直接返回原结果（幂等命中），不产生第二条切片；未办结请求保留阶段现场可续。
- 切片按归档存储版本 v2 及兼容数据取实际计算输入，明细含原归档号、样本版次、证据指纹与原摘要。

环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ARCHIVE_DB_PATH` | `backend/data/archive.sqlite3` | 迁移库 SQLite 文件路径（部署指向持久卷） |
| `ARCHIVE_CREDENTIAL_SECRET` | 开发占位密钥 | 凭证 HMAC 密钥，生产必须注入 |
| `ARCHIVE_AUTO_RECOVERY` | `1` | 启动时是否自动续办未办结恢复（测试可置 `0`） |

测试（仅依赖标准库与现有 venv）：

```bash
cd backend
ARCHIVE_AUTO_RECOVERY=0 .venv/bin/python -m unittest discover -s app.tests -v
```

领域：数据工程
子方向：数据分析与数据应用
