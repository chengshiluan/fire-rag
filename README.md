# fire-rag — 消防知识库 RAG 数据采集项目

> 构建面向消防领域的检索增强生成（RAG）知识库

## 目录结构

```
fire-rag/
├── README.md                # 本文件
├── docs/
│   ├── research-plan.md     # 调研计划（主文档）
│   └── task-list.md         # 任务跟踪清单
├── scripts/                 # 采集脚本（待开发）
├── data/                    # 采集数据（分 5 类存放）
│   ├── laws/                # 法律/行政法规
│   ├── standards/           # 国家标准（GB）
│   ├── local_regulations/   # 地方性法规/标准
│   ├── industry_guidelines/ # 行业规程/技术指南
│   └── supplementary/       # 补充资料
└── research_outputs/        # 调研产出物
```

## 快速导航

| 文档 | 用途 |
|------|------|
| [PRD 原始需求](docs/prd.md) | 需求方原始需求文档 |
| [调研计划](docs/research-plan.md) | 调研阶段划分、任务分解、风险评估 |
| [任务清单](docs/task-list.md) | 可执行的任务列表与进度跟踪 |

## 数据源速查

| 数据源 | 类别 | 采集难度 |
|--------|------|----------|
| flk.npc.gov.cn | 法律/行政法规 | 低 |
| openstd.samr.gov.cn | 国家标准（GB） | **极高** |
| mem.gov.cn | 部委规章/政策文件 | 中 |
| 各地方网站 | 地方性法规/标准 | 中高 |

## 项目阶段

1. **调研验证**（当前阶段）— 验证各数据源可行性
2. **工程开发** — 批量采集 + 清洗切分
3. **入库验证** — 向量化入库 + 检索效果验证

---

> 版本 v0.1 | 2026-06-12
