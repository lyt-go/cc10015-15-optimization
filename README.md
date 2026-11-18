# 多维分析实验平台

面向实验设计、指标计算、样本分析、效果评估与结论发布的数据分析实验平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
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

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 实验设计 | `experiment` | 实验方案 | 方案编号、实验名称、对照组说明 |
| 样本分群 | `sample_group` | 样本分群 | 分群编号、分群名称、筛选条件 |
| 指标定义 | `metric_def` | 指标定义 | 指标编号、指标名称、计算公式 |
| AB结果分析 | `ab_result` | 分析报告 | 报告编号、实验编号、分析指标 |
| 分群对比 | `segment_compare` | 对比任务 | 任务编号、分群A、分群B |
| 多维下钻 | `drill_down` | 下钻维度 | 维度编号、维度名称、下钻层级 |
| 数据筛选 | `data_filter` | 筛选规则 | 规则编号、数据来源、筛选字段 |
| 归因分析 | `attr_analysis` | 归因任务 | 任务编号、目标指标、候选因子 |
| 异常检测 | `anomaly_detect` | 检测规则 | 规则编号、监控指标、检测算法 |
| 预测模型 | `forecast_model` | 模型配置 | 模型编号、预测对象、历史窗口 |
| 数据导出 | `data_export` | 导出任务 | 任务编号、数据来源、导出格式 |
| 同期群追踪 | `cohort_track` | 同期群定义 | 群组编号、群组名称、首次行为 |
| 可视化配置 | `chart_config` | 图表配置 | 图表编号、图表类型、数据来源 |
| 洞察报告 | `insight_report` | 洞察报告 | 报告编号、分析主题、数据来源 |
| 数据新鲜度 | `data_freshness` | 新鲜度监控 | 监控编号、数据表名、最新分区 |
| 口径注册 | `metric_alias` | 口径版本 | 版本编号、指标编号、变更说明 |
| 实验因子 | `expr_factor` | 因子配置 | 因子编号、因子名称、因子层级 |
| 评估看板 | `evaluate_board` | 看板配置 | 看板编号、看板名称、关联实验 |
| 因子约束分配 | `factor_allocation` | 因子约束分配表 | 分配批次、缓存批次、实验编号、规则版本 |

## 因子约束分配（V015）

整组生成因子约束分配表，按批次复用已校验约束：

- 读取：一批分配只在开头读一次实验表、一次因子配置表，同批次内所有因子组复用同一份
  快照，实验规模变大时读取次数不随行数增长（`store.read_rows` 记扫描次数）。
- 缓存：`factor_constraint_cache` 以「因子组 + 因子摘要」为键保存校验结果与规则版本，
  约束缓存与规则版本同一条落库；同一组旧版本打失效标记保留差异。
- 分配表：`factor_allocation_batch` 是整组一次提交的批次头，`factor_allocation` 逐条回执，
  每条明细都带 `分配批次`（整组提交的批次）与 `缓存批次`（实际复用的约束缓存来源）。
- 增量：只改一个因子配置时只重算摘要变化的因子组，其他组明细与规则版本保持不变，
  缓存批次号可查。
- 中断续算：每个因子组单独提交，中断只回滚当前组；`retry` 只补未完成组，已完成组不回退。
- 版本冲突：`reuse` 保留旧缓存结果与批次号，把补算实验单列；`rebuild` 清空缓存整批重建，
  旧批次明细与旧缓存差异写入 `factor_allocation_archive` 留档。
- 核对：`GET /api/factor_allocation/verify` 校验明细与缓存一一对齐、同组同版本不并行
  两套缓存批次、批次头明细数与实际一致。
- 联动：指标定义页（V015）通过 `GET /api/metric_def/factor_layers` 读取分配表中的实验编号、
  缓存批次与因子层，核对指标口径。

主要接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/factor_allocation/generate` | 生成分配表，可带 `scope`、`strategy`、`dry_run` |
| POST | `/api/factor_allocation/retry` | 中断后按未完成组补算 |
| POST | `/api/factor_allocation/rebuild` | 清空缓存后整批重建并留档 |
| GET | `/api/factor_allocation` | 分配明细列表（含详情字段） |
| GET | `/api/factor_allocation/status` | 在册批次、缓存条数与配置版本 |
| GET | `/api/factor_allocation/verify` | 事务残留核对 |
| GET | `/api/factor_allocation/consumer` | 下游消费视图（实验编号、缓存批次、因子层） |
| PUT | `/api/expr_factor/{id}` | 修改单条因子配置并只重算受影响组 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据分析与数据应用
