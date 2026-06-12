# 消防 RAG 知识库 — 反爬策略评估、合规方案与数据采集架构设计

> 版本 v1.0 | 日期 2026-06-12 | 基于 PRD v0.1 与研究计划 v0.1

---

## Part A：各数据源反爬策略评估

### A.1 国家法律法规数据库 (flk.npc.gov.cn)

#### A.1.1 Robots.txt 检查

该站点的 `/robots.txt` 路径**没有返回有效的 robots 协议文件**。WebFetch 抓取只获得了 `<title>国家法律法规数据库</title>` 的 HTML 片段，说明该路径可能由前端 SPA 路由接管，不存在独立的 robots.txt。从实际效果判断，该站点**未通过 robots.txt 声明任何爬取限制**。

#### A.1.2 反爬机制评估

| 机制 | 是否存在 | 严重程度 | 说明 |
|------|---------|---------|------|
| 频率限制 | **存在** | 中 | API 高频请求会触发 504 Gateway Timeout，需要 3-10 秒的间隔 |
| 会话验证 | **存在** | 高 | 批量下载/导出必须通过浏览器点击触发，后端不接受直接的 API 调用 |
| 验证码 | **存在** | 高 | 部分操作触发验证码，headless 模式下更易被识别 |
| Headless 检测 | **存在** | 高 | 社区报告确认 headless 模式被识别为机器人，需使用有界面模式 |
| IP 封禁 | 可能 | 中 | 长期高频请求存在被封禁风险 |
| 登录要求 | **不需要** | 低 | 检索和查看法律文本无需登录 |
| Cookie/Token | **需要** | 中 | API 调用需要携带合理的 Cookie，否则部分接口返回空 |

#### A.1.3 技术接口分析

**列表检索 API：**

```
GET https://flk.npc.gov.cn/api/?type={type}&searchType=title;vague&sortTr=f_bbrq_s;desc&gbrqStart=&gbrqEnd=&sxrqStart=&sxrqEnd=&sort=true&page={page}&size=10&_={timestamp}
```

参数说明：
- `type`: `fl`（法律）、`xzfg`（行政法规）、`dfxfg`（地方性法规）、`sfjs`（司法解释）、`jcfg`（监察法规）、`flfg`（全部法律法规）
- `searchType`: `title;vague`（标题模糊搜索）、`title;accurate`（标题精确搜索）
- `page`: 页码（从 1 开始）
- `size`: 每页条数（最大约 10-20）
- `_`: 时间戳（防缓存）

**详情 API：**

```
POST https://flk.npc.gov.cn/api/detail
Body: id={法规ID}
```

**文件下载：**

```
https://wb.flk.npc.gov.cn/{相对路径}   （相对路径从详情 API 的 body[0].path 字段获取）
```

文件格式优先选择：DOCX > HTML > PDF。

#### A.1.4 合规采集策略

| 策略项 | 方案 |
|--------|------|
| **采集方式** | 优先使用 Playwright 有界面模式（`headless: false`），模拟真人操作 |
| **请求间隔** | 列表翻页：3-5 秒；详情抓取：5-10 秒；下载文件：10-15 秒 |
| **User-Agent** | 使用真实浏览器 UA，如 Chrome 120+ `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36` |
| **代理池** | **不需要**。该站点为官方公共服务，合理频率下不需要代理 |
| **并发控制** | 单线程顺序请求，不使用线程池（ThreadPoolExecutor 会触发限流） |
| **重试策略** | 遇到 504：等待 30s 后重试，最多 3 次；遇到验证码：暂停 5 分钟，切换为手动介入 |
| **断点续传** | 每页处理完记录 `last_page` 到 checkpoint 文件，中断后从该页恢复 |

#### A.1.5 针对"消防"关键字的检索方案

```
GET https://flk.npc.gov.cn/api/?type=flfg&searchType=title;vague&keyword=消防&sortTr=f_bbrq_s;desc&page=1&size=10
```

或者使用正文检索（如果 API 支持 `searchType=body;vague`），覆盖标题中不含"消防"但内容涉及消防的法规（如《安全生产法》）。

---

### A.2 国家标准全文公开系统 (openstd.samr.gov.cn)

#### A.2.1 Robots.txt 检查

`/robots.txt` 返回 **HTTP 404 Not Found**，不存在 robots 协议文件。

#### A.2.2 反爬机制评估

| 机制 | 是否存在 | 严重程度 | 说明 |
|------|---------|---------|------|
| 频率限制 | 可能 | 低 | 纯 HTML 页面结构，无明显的速率限制 |
| 验证码 | 未发现 | — | 浏览标准列表和详情无需验证码 |
| 登录要求 | **不需要** | 低 | 公开浏览标准题录和已公开全文 |
| Headless 检测 | 未发现 | 低 | 页面为传统服务端渲染（非 SPA），不做 JS 检测 |
| IP 封禁 | 可能 | 中 | PDF 大量下载可能触发 |
| 水印 | **存在** | 中 | 在线预览为标准文本带水印的分页图片，非文本型 PDF |

#### A.2.3 技术接口分析

**列表 API：**

```
GET http://www.gb688.cn/bzgk/gb/std_list_type?r={random}&page={page}&pageSize=10&p.p1=1&p.p90=circulation_date&p.p91=desc
```

- `p.p1=1`：强制性国家标准 GB
- `p.p1=2`：推荐性国家标准 GB/T
- `p.p1=3`：指导性技术文件 GB/Z

**详情页：**

```
GET http://www.gb688.cn/bzgk/gb/newGbInfo?hcno={GUID}
```

**PDF 获取：**

```
GET http://c.gb688.cn/bzgk/gb/showGb?type=download&hcno={GUID}    # 下载
GET http://c.gb688.cn/bzgk/gb/viewGb?hcno={GUID}                  # 在线预览
```

#### A.2.4 合规性评估（关键）

**版权声明（原文引用）：**

> "本系统所提供的电子文本仅供个人学习、研究之用，未经授权，禁止复制、发行、汇编、翻译或网络传播等，侵权必究。"

**国家市场监管总局官方表态（2025 年）：**

> "国家标准凝聚了众多专家学者、社会公众及专业机构的智慧与努力，依法受《著作权法》保护。保护标准版权不仅是对创作者劳动成果的尊重，也是落实国家知识产权战略的重要举措。我们将依法打击侵犯国家标准版权的违法行为，保障标准的权威性和公信力。"

**合规结论：**

- **大规模批量复制 GB 文本用于商业知识库存在极高版权风险**。即使技术可行，法律上可能构成侵权行为。
- 2025 年 2 月起，非采标国家标准已开放免费下载，但仅限于"个人学习、研究"，未授权汇编和网络传播。
- **强烈建议采用以下替代方案：**
  1. **正版采购**：联系中国标准出版社或标准在线平台，获取商业授权电子版
  2. **限缩范围**：只采录 GB 的名称、编号、章节标题等题录信息，引导用户到官方系统查看原文
  3. **使用公开摘要**：仅收录标准中已公开的题录和概述信息
  4. **RAG 链接模式**：知识库只索引标准名称和编号，检索时返回引用信息，用户自行查阅原文

#### A.2.5 合规采集策略（如果取得授权）

| 策略项 | 方案 |
|--------|------|
| **采集方式** | requests + BeautifulSoup（传统服务端渲染页面） |
| **请求间隔** | 标准列表翻页：2-3 秒；PDF 下载：10-30 秒 |
| **User-Agent** | 标准浏览器 UA |
| **代理池** | 不需要 |
| **OCR 方案** | 如果 PDF 为扫描件/图片，使用 PaddleOCR 进行识别，表格区域使用 PP-Structure |
| **重试策略** | 失败等待 10s 重试，最多 3 次 |

---

### A.3 应急管理部官网 (mem.gov.cn)

#### A.3.1 Robots.txt 检查

- `mem.gov.cn/robots.txt`：**连接被拒绝**（ECONNREFUSED）
- `www.mem.gov.cn/robots.txt`：**HTTP 404 Not Found**

不存在有效的 robots 协议文件。

#### A.3.2 反爬机制评估

| 机制 | 是否存在 | 严重程度 | 说明 |
|------|---------|---------|------|
| 频率限制 | 极低 | 低 | 政府信息公开网站，未发现明显限流 |
| 验证码 | 未发现 | — | 浏览法规栏目无需验证码 |
| 登录要求 | **不需要** | 低 | 信息公开为主动公开，无需登录 |
| IP 封禁 | 未发现 | 低 | 合理频率下不会触发 |
| JavaScript 渲染 | 不需要 | 低 | 服务端渲染的 HTML 页面 |

#### A.3.3 网站结构分析

**法规栏目 URL 结构：**

| 栏目 | URL 模式 |
|------|---------|
| 法律 | `https://www.mem.gov.cn/fw/flfgbz/fg/fl_6143/` |
| 行政法规 | `https://www.mem.gov.cn/fw/flfgbz/fg/xzfg_6144/` |
| 司法解释 | `https://www.mem.gov.cn/fw/flfgbz/fg/sfjs_6145/` |
| 规章 | `https://www.mem.gov.cn/fw/flfgbz/gz/` |
| 标准文本 | `https://www.mem.gov.cn/fw/flfgbz/bz/bzwb/index_1.shtml` |
| 规范性文件 | `https://www.mem.gov.cn/fw/flfgbz/gfxwj/` |
| 政策解读 | `https://www.mem.gov.cn/gk/zcjd/index_2.shtml` |

**分页规律**：`index.shtml`（第 1 页）、`index_1.shtml`（第 2 页）、`index_2.shtml`（第 3 页）...

**内容格式**：
- 列表页为静态 HTML `<ul>/<li>` 结构，包含标题链接和发布日期
- 详情页为 HTML 正文，或提供 PDF 下载链接
- PDF 类型待抽样确认（文本型 vs 扫描型）

#### A.3.4 合规采集策略

| 策略项 | 方案 |
|--------|------|
| **采集方式** | requests + BeautifulSoup/lxml |
| **请求间隔** | 2-5 秒（政府网站，不必过快） |
| **User-Agent** | 标准浏览器 UA |
| **代理池** | 不需要 |
| **增量更新** | 按"发布日期"字段识别新法规 |
| **重试策略** | 失败等待 5s 重试，最多 3 次 |

---

### A.4 地方人大/应急管理局网站

#### A.4.1 总体评估

地方网站差异极大，无法统一描述。根据调研（北京、上海、广州、福州、浙江），归纳如下：

| 特征 | 常见情况 | 备注 |
|------|---------|------|
| 网站结构 | 传统 CMS（如政府网站群系统） | URL 通常包含栏目路径和文章 ID |
| 内容格式 | HTML 正文（以 `<div>` 或 `<table>` 包裹条款） | 也有 PDF 附件形式 |
| 检索功能 | 站内搜索，部分有独立法规库 | 搜索接口各异 |
| 反爬措施 | 普遍较弱 | 大多数为静态 HTML |
| Robots.txt | 多数不存在或 Allow 全站 | 政府公开信息 |
| 验证码 | 极少 | 部分网站搜索功能可能有验证码 |

#### A.4.2 典型 URL 模式

```
# 福州市人大
https://rd.fuzhou.gov.cn/fzdfxfg/{year}{month}/t{date}_{id}.htm

# 北京市商务局（转载）
https://sw.beijing.gov.cn/zt/swaqzl/fgtl/{year}{month}/t{date}_{id}.html

# 浙江省（PDF 下载）
https://zjjcmspublic.oss-cn-hangzhou-zwynet-d01-a.internet.cloud.zj.gov.cn/.../xxx.pdf

# 广州市规划和自然资源局
https://ghzyj.gz.gov.cn/zwgk/newzcfg/ywly/content/mpost_{id}.html
```

#### A.4.3 合规采集策略

| 策略项 | 方案 |
|--------|------|
| **采集方式** | 每个站点编写独立 Adapter（继承 BaseCrawler） |
| **请求间隔** | 2-5 秒 |
| **User-Agent** | 标准浏览器 UA |
| **代理池** | 不需要 |
| **处理方式** | HTML 正文直接解析；PDF 附件用 pdfplumber 提取 |
| **优先级** | 先覆盖业务涉及的 3-5 个重点省市 |

---

### A.5 合规声明总结

#### A.5.1 法律依据

| 依据 | 内容 |
|------|------|
| 《中华人民共和国著作权法》第五条 | "本法不适用于：......（一）法律、法规，国家机关的决议、决定、命令和其他具有立法、行政、司法性质的文件，及其官方正式译文" |
| 《政府信息公开条例》 | 行政机关应当主动公开政府信息 |
| 《标准化法》第十七条 | "强制性标准文本应当免费向社会公开。国家推动免费向社会公开推荐性标准文本。" |

#### A.5.2 各类数据合规判断

| 数据类别 | 合规状态 | 依据 | 注意事项 |
|---------|---------|------|---------|
| 法律、行政法规、部门规章 | **合法采集** | 著作权法第五条，不受版权保护 | 标注来源 |
| 地方性法规 | **合法采集** | 同上，属"地方国家机关的决议" | 标注来源 |
| 司法解释 | **合法采集** | 同上，属"司法性质的文件" | 标注来源 |
| 强制性国家标准（GB） | **需谨慎** | 公开文本免费，但标准整体受版权保护 | 建议题录索引+链接，或取得授权 |
| 推荐性国家标准（GB/T） | **需授权** | 著作权法保护，官方声明禁止传播 | 强烈建议正版采购 |
| 行业标准（AQ、YJ 等） | **需评估** | 需单独确认各行业标准版权政策 | 建议题录索引 |
| 政府主动公开的政策文件 | **合法采集** | 政府信息公开条例 | 标注来源 |

#### A.5.3 数据采集伦理准则

1. **尊重数据源服务能力**：控制请求频率，不对目标服务器造成压力
2. **保持数据完整性**：不篡改原文内容，标注修订版本信息
3. **标注来源与时效**：每条数据标注来源 URL、发布机关、发布日期
4. **不绕过安全措施**：不破解验证码、不绕过 IP 封禁
5. **遵守 robots 协议精神**：虽大部分站点无 robots.txt，仍遵守合理抓取伦理
6. **版权数据不扩散**：对于受版权保护的 GB 数据，不对外再分发原始文本

---

## Part B：数据采集架构设计

### B.1 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                      数据采集架构                              │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐    │
│  │ flk 采集器 │  │ GB 采集器  │  │ MEM 采集器 │  │ 地方采集器  │    │
│  │ (Adapter) │  │ (Adapter) │  │ (Adapter) │  │ (Adapter) │    │
│  └─────┬─────┘  └─────┬─────┘  └─────┬─────┘  └─────┬─────┘    │
│        │              │              │              │           │
│  ┌─────┴──────────────┴──────────────┴──────────────┴─────┐    │
│  │                   BaseCrawler 基类                       │    │
│  │  ┌─────────┐ ┌──────────┐ ┌────────┐ ┌─────────────┐   │    │
│  │  │ Session  │ │ 频率控制  │ │ 重试器  │ │ 断点续传     │   │    │
│  │  │ 管理     │ │ RateLimit│ │ Retryer │ │ Checkpoint   │   │    │
│  │  └─────────┘ └──────────┘ └────────┘ └─────────────┘   │    │
│  │  ┌─────────┐ ┌──────────┐ ┌────────┐ ┌─────────────┐   │    │
│  │  │ 日志系统  │ │ 数据清洗  │ │ JSON输出│ │ 配置管理     │   │    │
│  │  │ Logger   │ │ Cleaner  │ │ Dumper  │ │ Config       │   │    │
│  │  └─────────┘ └──────────┘ └────────┘ └─────────────┘   │    │
│  └──────────────────────────┬──────────────────────────────┘    │
│                             │                                    │
│              ┌──────────────┴──────────────┐                    │
│              │       数据流水线             │                    │
│              └──────────────┬──────────────┘                    │
│                             │                                    │
│    采集层 ─────▶ 清洗层 ─────▶ 结构化层 ─────▶ 存储层            │
│    原始HTML/    HTML去标签    按章程切分      JSON/数据库          │
│    PDF/DOCX     文本规范化    提取元数据      向量化入库            │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### B.2 爬虫基类设计 (BaseCrawler)

位于 `/root/fire-rag/scripts/crawler_base.py`，提供所有 Adapter 共享的基础设施：

| 模块 | 功能 | 关键方法 |
|------|------|---------|
| `RequestManager` | Session 管理、Cookie 持久化、请求/响应拦截 | `get()`, `post()`, `download_file()` |
| `RateLimiter` | 基于令牌桶算法的频率控制 | `wait()` |
| `RetryHandler` | 指数退避重试 + 错误分类 | `execute_with_retry()` |
| `CheckpointManager` | JSON 文件断点续传 | `save()`, `load()`, `mark_complete()` |
| `CrawlLogger` | 结构化日志（JSON 行格式） | `log_crawl()`, `log_error()`, `log_stat()` |
| `TextCleaner` | 数据清洗工具函数集 | `clean_html()`, `normalize_text()`, `extract_date()` |
| `DataDumper` | 统一 JSON 输出格式 | `dump_law()`, `dump_batch()` |

### B.3 数据流水线详情

#### 第 1 层：采集层 (Crawl Layer)

```
输入: URL 列表（来自检索/列表抓取）
处理:
  - 列表页抓取 → 解析获得详情 URL 列表
  - 详情页抓取 → 下载 HTML/PDF/DOCX 原始文件
  - 文件下载 → 保存原始文件到 raw/ 目录
输出: {data_dir}/raw/{source_name}/{doc_id}.{html|pdf|docx}
      元数据索引: {data_dir}/raw/{source_name}/index.jsonl
```

#### 第 2 层：清洗层 (Clean Layer)

```
输入: 原始文件（HTML/PDF/DOCX）
处理:
  - HTML: BeautifulSoup 提取正文，去除导航/广告/页脚
  - PDF: pdfplumber 提取文本 + 表格
  - DOCX: python-docx 提取段落
  - 文本规范化: 全角半角统一、多余空白清理、特殊字符处理
  - PDF 扫描件: PaddleOCR 识别（条件性启用）
输出: {data_dir}/cleaned/{source_name}/{doc_id}.txt
      + 清洗日志 {data_dir}/cleaned/{source_name}/clean_log.jsonl
```

#### 第 3 层：结构化层 (Structure Layer)

```
输入: 清洗后的纯文本
处理:
  - 法规结构识别: 正则匹配 "第X章"、"第X条"、"第X款" 标记
  - 国标结构识别: 匹配 "X 范围"、"X 规范性引用文件" 等章节头
  - 元数据提取: 标题、发布机关、发布日期、生效日期、层级
  - 内容哈希: SHA256 用于去重和增量检测
  - 切分为 chunk（按条/款/项，保留元数据引用链）
输出: {data_dir}/structured/{source_name}/{doc_id}.json
      符合统一 Schema（见 Part C）
```

#### 第 4 层：存储层 (Storage Layer)

```
输入: 结构化 JSON 文档
处理:
  - 写入 JSON 文件存储库
  - 可选: 导入向量数据库（Milvus/Qdrant/Chroma）
  - 可选: 导入 Elasticsearch（全文检索）
输出:
  - JSON 知识库: {data_dir}/knowledge_base/
  - Vector DB: chunk 向量 + 元数据
  - 索引更新
```

### B.4 增量更新策略

#### B.4.1 新法规检测

| 数据源 | 检测方法 | 实现方式 |
|--------|---------|---------|
| flk.npc.gov.cn | 按"公布日期"倒序拉取，与本地已采集集合对比 | 记录 `last_publish_date`，只爬取 `publish > last_publish_date` |
| openstd.samr.gov.cn | 监测公告栏目 / 标准发布通知 | 定期扫描新公告，对比本地标准号集合 |
| mem.gov.cn | 列表页按发布日期排序，对比最新记录 | 记录每个栏目的 `latest_crawl_date` |
| 地方网站 | 定期全量扫描法规栏目 | 对比 `content_hash` 识别增量和变更 |

#### B.4.2 已有数据更新流程

```
1. 增量扫描 → 发现新 URL 或 content_hash 变化
2. 对变更项重新执行采集→清洗→结构化流水线
3. 保留旧版本在 history/ 目录（版本追踪）
4. 更新知识库索引
5. 记录更新时间到 metadata 的 updated_at 字段
```

#### B.4.3 调度策略

- **全量采集**：项目初始化执行一次
- **增量采集**：每周一次（周一凌晨），使用 cron 或 GitHub Actions
- **法规更新监测**：每日一次轻量检查（只拉列表页，比对数量）
- **GB 更新监测**：与国家标准发布公告周期同步（约每月一次）

---

## Part C：数据 Schema 设计

详见 `/root/fire-rag/docs/data_schema.md`。

---

## Part D：公共工具模块

### D.1 爬虫基类

位于 `/root/fire-rag/scripts/crawler_base.py`，包含：
- `BaseCrawler` 类
- 数据清洗工具函数
- JSON 输出工具

### D.2 依赖清单

位于 `/root/fire-rag/scripts/requirements.txt`。

---

## 附录：目录结构建议

```
/root/fire-rag/
├── scripts/
│   ├── crawler_base.py          # 爬虫基类
│   ├── requirements.txt          # Python 依赖
│   ├── adapters/
│   │   ├── flk_adapter.py       # flk.npc.gov.cn 适配器
│   │   ├── mem_adapter.py       # mem.gov.cn 适配器
│   │   ├── gb_adapter.py        # openstd.samr.gov.cn 适配器
│   │   └── local_adapter.py     # 地方网站适配器基类
│   └── utils/
│       ├── pdf_extractor.py     # PDF 提取工具
│       └── ocr_engine.py        # OCR 引擎封装
├── data/
│   ├── raw/                     # 原始下载文件
│   ├── cleaned/                 # 清洗后文本
│   ├── structured/              # 结构化 JSON
│   └── knowledge_base/          # 最终知识库
├── config/
│   ├── data_sources.yaml        # 数据源配置
│   └── fire_law_list.yaml       # 消防法规采集清单
├── checkpoints/                 # 断点续传文件
├── logs/                        # 采集日志
├── docs/
│   ├── prd.md
│   ├── research-plan.md
│   ├── task-list.md
│   └── data_schema.md
└── research_outputs/
    └── compliance_and_architecture_report.md  # 本文件
```

---

> 本报告基于 PRD v0.1 编写，供项目开发团队参考。数据源的实际情况可能随时间变化，建议在正式开始采集前对各站点进行一次实地验证。
