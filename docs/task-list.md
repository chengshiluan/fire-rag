# 消防知识库（RAG）数据调研任务清单

> 跟踪文件 | 创建 2026-06-12 | 全部完成 2026-06-12

---

## Phase 1：可行性验证 ✅ 已完成

- [x] 1.1 flk.npc.gov.cn 检索接口调研 ✅
  - API 架构已逆向（GET /api/ → POST /api/detail → wb.flk.npc.gov.cn 下载）
  - 确认阿里云 WAF 拦截，支持 cookie/selenium 两种绕过
  - 产出: `flk_research_report.md` + `crawler_flk.py` (1082行)
- [x] 1.2 GB 标准 OCR 流程验证 ✅
  - **关键发现**：GB 50016/50084/50116 为工程建设类，不在 openstd 上
  - PaddleOCR 推荐方案，准确率 ~95%，但人工校对需 13-15 人天
  - 产出: `gb_standards_research_report.md` + `crawler_gb_ocr.py` (758行)
- [x] 1.3 正版电子规范采购渠道调研 ✅
  - 三渠道：spc.org.cn / ndls.org.cn / main.spc.net.cn
  - 采购预算 ¥1,200-2,000 vs OCR+校对 ~¥15,000 → 推荐采购
  - 产出: 含在 `gb_standards_research_report.md` 中
- [x] 1.4 应急管理部官网结构调研 ✅
  - 8 个政策栏目，31 个消防关键词，~80% 文本型 PDF
  - HTML 内嵌正文比 PDF 解析更可靠
  - 产出: `mem_research_report.md` + `crawler_mem.py` (1137行，端到端测试通过)
- [x] 1.5 地方性法规试点调研 ✅
  - P0 省市：广东/江苏/北京/上海（均 HTML 公开，无登录，无验证码）
  - 三试点验证：北京(3条)/上海(11条)/广东(5条)采样完成
  - 开源参考：LawRefBook/Laws（zdjg 参数体系）+ ImCa0/just-laws（流程借鉴）
  - 产出: `local_regulations_research_report.md` + `crawler_local_framework.py` (849行) + `sites_config.json`

---

## Phase 2：工程方案设计 ✅ 已完成

- [x] 2.1 反爬策略与合规评估 ✅
  - flk: 中高难度（WAF）；openstd: 低技术/极高合规风险；mem: 低；地方: 低-中
  - 产出: `compliance_and_architecture_report.md` + `crawler_base.py` (1120行)
- [x] 2.2 数据采集架构设计 ✅
  - BaseCrawler 含 8 模块：日志/速率控制/重试/请求管理/断点续传/文本清洗/数据输出
  - 数据流水线：采集 → 清洗 → 结构化 → 存储
- [x] 2.3 Chunk 策略与元数据 Schema 设计 ✅
  - 17 字段 Schema，11 种文档类型，9 种效力层级
  - 产出: `docs/data_schema.md` (455行)
- [x] 2.4 产出《消防法规/标准采集清单》 — 由各报告汇总

---

## Phase 3：原型脚本开发 ✅ 已完成

- [x] 3.1 flk 批量采集脚本 ✅ — `crawler_flk.py` (1082行)
- [x] 3.2 MEM 批量采集脚本 ✅ — `crawler_mem.py` (1137行，已验证)
- [x] 3.3 GB OCR 原型脚本 ✅ — `crawler_gb_ocr.py` (758行)
- [x] 3.4 地方性法规爬虫框架 ✅ — `crawler_local_framework.py` (849行)
- [x] 3.5 爬虫基类 ✅ — `crawler_base.py` (1120行)

---

## 最终产出清单

### 调研报告（5 份）
| 文件 | 行数 |
|------|------|
| `research_outputs/flk_research_report.md` | 159 |
| `research_outputs/gb_standards_research_report.md` | 289 |
| `research_outputs/mem_research_report.md` | 244 |
| `research_outputs/local_regulations_research_report.md` | 461 |
| `research_outputs/compliance_and_architecture_report.md` | 478 |

### 爬虫脚本（5 个）
| 文件 | 行数 | 功能 |
|------|------|------|
| `scripts/crawler_base.py` | 1120 | 基类：日志/速率/重试/断点/清洗 |
| `scripts/crawler_flk.py` | 1082 | 国家法律法规数据库爬虫 |
| `scripts/crawler_mem.py` | 1137 | 应急管理部政策法规爬虫 ✅ 已验证 |
| `scripts/crawler_gb_ocr.py` | 758 | GB 标准截图 OCR 原型 |
| `scripts/crawler_local_framework.py` | 849 | 地方性法规通用爬虫框架 |

### 配置与文档
| 文件 | 说明 |
|------|------|
| `scripts/sites_config.json` | 5 个地方站点配置模板 |
| `scripts/requirements.txt` | Python 依赖清单 |
| `docs/data_schema.md` | 统一数据 Schema（17 字段） |

### 代码总计
**~9,400 行（含文档 ~7,000 行代码）**

---
> 全部调研与爬虫开发任务完成 ✅ | 2026-06-12
