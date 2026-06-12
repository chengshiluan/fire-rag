# 消防 RAG 知识库 — 统一数据 Schema

> 版本 v1.0 | 日期 2026-06-12

本文档定义了所有采集数据必须遵循的统一元数据 Schema。所有 Adapter 的 `parse_detail()` 方法必须输出符合此 Schema 的字典。

---

## 1. 顶层结构

```json
{
  "id": "唯一标识（SHA256 前 16 位或来源系统 ID）",
  "title": "法规/标准名称",
  "doc_type": "文档类型枚举",
  "publisher": "发布机关",
  "publish_date": "发布日期（YYYY-MM-DD）",
  "effective_date": "生效日期（YYYY-MM-DD）",
  "status": "现行状态枚举",
  "source_url": "来源 URL",
  "source_name": "数据源名称",
  "hierarchy": "效力层级枚举",
  "chapters": [],
  "full_text": "全文纯文本",
  "crawl_time": "采集时间（ISO 8601 UTC）",
  "content_hash": "内容 SHA256 哈希",
  "revisions": [],
  "keywords": [],
  "tags": []
}
```

---

## 2. 字段详细说明

### 2.1 `id` (string, 必填)

文档唯一标识符。

**生成规则**：
- 优先使用来源系统提供的 ID（如 flk.npc.gov.cn 的 `id` 字段）
- 若无系统 ID，使用 `SHA256(来源名称 | 标题 | 发布日期)` 的前 16 位十六进制字符

**示例**：`"a1b2c3d4e5f6g7h8"`

---

### 2.2 `title` (string, 必填)

法规或标准的完整名称，去除前缀修饰语。

**清洗规则**：
- 去除 "中华人民共和国" 前缀（可选保留，由采集脚本决定）
- 去除多余的空白字符
- 保留标准编号（如 `GB 50016-2014`）

**示例**：
- `"消防法"` 或 `"中华人民共和国消防法"`
- `"建筑设计防火规范 GB 50016-2014"`
- `"北京市消防条例"`

---

### 2.3 `doc_type` (string, 必填)

文档类型，枚举值如下：

| 值 | 含义 | 典型来源 |
|----|------|---------|
| `law` | 法律 | flk.npc.gov.cn、mem.gov.cn |
| `administrative_regulation` | 行政法规 | flk.npc.gov.cn、mem.gov.cn |
| `departmental_rule` | 部门规章 | mem.gov.cn |
| `judicial_interpretation` | 司法解释 | flk.npc.gov.cn |
| `gb_standard` | 国家标准（GB/GB/T/GB/Z） | openstd.samr.gov.cn |
| `industry_standard` | 行业标准（AQ/YJ等） | mem.gov.cn、各行业网站 |
| `local_regulation` | 地方性法规 | 地方人大/应急厅网站 |
| `local_rule` | 地方政府规章 | 地方应急厅网站 |
| `policy_document` | 政策文件/规范性文件 | mem.gov.cn |
| `policy_interpretation` | 政策解读 | mem.gov.cn |
| `supplementary` | 补充资料（案例、FAQ等） | 各来源 |

---

### 2.4 `publisher` (string, 必填)

发布机关全称。

**示例**：
- `"全国人民代表大会常务委员会"`
- `"应急管理部"`
- `"国家市场监督管理总局、国家标准化管理委员会"`
- `"北京市人民代表大会常务委员会"`

---

### 2.5 `publish_date` (string, 必填)

发布日期，格式 `YYYY-MM-DD`。

如果只能获取到年月，补 `-01` 作为日期。

**示例**：`"2021-04-29"`（消防法 2021 修订版）

---

### 2.6 `effective_date` (string, 必填)

生效日期，格式 `YYYY-MM-DD`。

若未明确，可设为与 `publish_date` 相同（法规通常在公布之日起施行）。

**示例**：`"2021-04-29"`

---

### 2.7 `status` (string, 必填)

现行状态，枚举值：

| 值 | 含义 |
|----|------|
| `现行有效` | 当前有效 |
| `已废止` | 已明令废止 |
| `已被修改` | 部分条款已被修改但整体仍有效 |
| `部分失效` | 部分条款失效 |
| `尚未生效` | 已公布但未到生效日期 |
| `unknown` | 状态未知 |

**示例**：`"现行有效"`

---

### 2.8 `source_url` (string, 必填)

原始网页 URL。用于后续溯源验证。

**示例**：`"https://flk.npc.gov.cn/detail2.html?..."` 或原始详情页 URL

---

### 2.9 `source_name` (string, 必填)

数据源标识名（与 `BaseCrawler.source_name` 一致）。

| 值 | 含义 |
|----|------|
| `flk.npc.gov.cn` | 国家法律法规数据库 |
| `openstd.samr.gov.cn` | 国家标准全文公开系统 |
| `www.mem.gov.cn` | 应急管理部官网 |
| `rd.fuzhou.gov.cn` 等 | 各地方人大/应急厅网站 |

---

### 2.10 `hierarchy` (string, 必填)

效力层级，枚举值：

| 值 | 含义 | 对应 doc_type |
|----|------|-------------|
| `法律` | 全国人大及常委会制定的法律 | `law` |
| `行政法规` | 国务院制定的行政法规 | `administrative_regulation` |
| `部门规章` | 国务院部委制定的规章 | `departmental_rule` |
| `司法解释` | 最高法/最高检司法解释 | `judicial_interpretation` |
| `国家标准` | GB/GB/T/GB/Z | `gb_standard` |
| `行业标准` | AQ/YJ/XF 等行业标准 | `industry_standard` |
| `地方性法规` | 省级/设区的市人大制定的法规 | `local_regulation` |
| `地方政府规章` | 地方政府制定的规章 | `local_rule` |
| `规范性文件` | 行政机关发布的规范性文件 | `policy_document` |

---

### 2.11 `chapters` (array, 必填)

法规的章节结构，按顺序排列。如果法规不分章节，则包含一个 `chapter_title` 为空的章节，`articles` 中包含所有条款。

```json
[
  {
    "chapter_title": "第一章 总则",
    "articles": [
      {
        "article_no": "第一条",
        "content": "为了预防火灾和减少火灾危害，加强应急救援工作，保护人身、财产安全，维护公共安全，制定本法。"
      },
      {
        "article_no": "第二条",
        "content": "消防工作贯彻预防为主、防消结合的方针..."
      }
    ]
  }
]
```

#### `chapter_title` (string)

章节标题。

**示例**：
- `"第一章 总则"`
- `"第四章 灭火救援"`
- `""` （空字符串，表示没有章节标题的直接条款排列）

#### `articles` (array)

该章节下的条款数组。

#### `article_no` (string)

条款编号。

**示例**：
- `"第一条"`、`"第二十条"`（法律法规）
- `"3.1"`、`"3.1.2"`（国标标准）
- `"一、"`、`"（一）"`（规范性文件中的款/项）

#### `content` (string)

条款全文（该条/款/项的完整文本，包含所有款和项）。

**注意**：
- 对于法律条文，`content` 包含该条下的所有款、项
- 如果条款内容超过 1500 字，建议在后续 Chunk 阶段进一步拆分为子条款粒度

---

### 2.12 `full_text` (string, 必填)

全文纯文本（去除 HTML 标签、PDF 格式信息后的连续文本）。

**用途**：
- 全文检索
- 内容去重（通过 content_hash）
- 语言模型长上下文输入

**格式要求**：
- UTF-8 编码
- 换行符统一为 `\n`
- 段落之间保留一个空行
- 去除页眉页脚、水印、页码等非正文内容

---

### 2.13 `crawl_time` (string, 必填)

采集时间，ISO 8601 格式（UTC 时区）。

**示例**：`"2026-06-12T08:30:00.000Z"`

---

### 2.14 `content_hash` (string, 必填)

`full_text` 的 SHA256 哈希值（64 位十六进制小写字符串）。

**用途**：
- 重复检测：同一法规从不同渠道采集时可识别为同一条目
- 变更检测：重新采集后发现 hash 变化 → 法规已修订
- 增量更新：比对新旧版本内容

**计算方式**：
```
content_hash = SHA256(full_text)
```

---

### 2.15 `revisions` (array, 可选)

修订历史记录，用于追踪法规版本变更。

```json
[
  {
    "revision_date": "2021-04-29",
    "revision_type": "修订",
    "description": "第十三届全国人民代表大会常务委员会第二十八次会议修订",
    "previous_hash": "abc123..."
  }
]
```

---

### 2.16 `keywords` (array, 可选)

人工或自动提取的关键词，用于增强检索召回。

**示例**：`["消防", "火灾预防", "灭火救援", "消防设施"]`

---

### 2.17 `tags` (array, 可选)

业务标签，按维度分类：

```json
[
  "消防设计审查",
  "消防验收",
  "建筑防火",
  "消防设施",
  "应急处置",
  "危化品管理",
  "法律责任"
]
```

---

## 3. 完整示例

### 3.1 法律类示例（消防法第一条）

```json
{
  "id": "e8a7b3c9d1f4a2b6",
  "title": "中华人民共和国消防法",
  "doc_type": "law",
  "publisher": "全国人民代表大会常务委员会",
  "publish_date": "2021-04-29",
  "effective_date": "2021-04-29",
  "status": "现行有效",
  "source_url": "https://flk.npc.gov.cn/detail2.html?ZmY4MDgxODE3NTJiN2Q0YzAxNzVlNjEzYzgxYjM0Mzk%3D",
  "source_name": "flk.npc.gov.cn",
  "hierarchy": "法律",
  "chapters": [
    {
      "chapter_title": "第一章 总则",
      "articles": [
        {
          "article_no": "第一条",
          "content": "为了预防火灾和减少火灾危害，加强应急救援工作，保护人身、财产安全，维护公共安全，制定本法。"
        },
        {
          "article_no": "第二条",
          "content": "消防工作贯彻预防为主、防消结合的方针，按照政府统一领导、部门依法监管、单位全面负责、公民积极参与的原则，实行消防安全责任制，建立健全社会化的消防工作网络。"
        }
      ]
    },
    {
      "chapter_title": "第二章 火灾预防",
      "articles": [
        {
          "article_no": "第八条",
          "content": "地方各级人民政府应当将包括消防安全布局、消防站、消防供水、消防通信、消防车通道、消防装备等内容的消防规划纳入城乡规划，并负责组织实施。城乡消防安全布局不符合消防安全要求的，应当调整、完善；公共消防设施、消防装备不足或者不适应实际需要的，应当增建、改建、配置或者进行技术改造。"
        }
      ]
    }
  ],
  "full_text": "中华人民共和国消防法\n\n第一章 总则\n\n第一条 为了预防火灾和减少火灾危害，加强应急救援工作，保护人身、财产安全，维护公共安全，制定本法。\n第二条 消防工作贯彻预防为主、防消结合的方针...\n\n第二章 火灾预防\n\n第八条 地方各级人民政府应当将包括消防安全布局...",
  "crawl_time": "2026-06-12T08:30:00.000Z",
  "content_hash": "e5b7c3a1f8d2e9a4b6c0d3f7a1b5c9e2d4f6a8b0c2d4e6f8a0b2c4d6",
  "revisions": [
    {
      "revision_date": "2021-04-29",
      "revision_type": "修订",
      "description": "第十三届全国人民代表大会常务委员会第二十八次会议修订",
      "previous_hash": null
    }
  ],
  "keywords": ["消防法", "消防", "火灾预防", "灭火救援", "消防安全责任制"],
  "tags": ["建筑防火", "消防设施", "消防监管", "灭火救援"]
}
```

### 3.2 国家标准类示例（GB 50016 节选）

```json
{
  "id": "d4f6a8b0c2e4d6f8",
  "title": "建筑设计防火规范 GB 50016-2014",
  "doc_type": "gb_standard",
  "publisher": "中华人民共和国住房和城乡建设部、国家质量监督检验检疫总局",
  "publish_date": "2014-08-27",
  "effective_date": "2015-05-01",
  "status": "现行有效",
  "source_url": "http://www.gb688.cn/bzgk/gb/newGbInfo?hcno=...",
  "source_name": "openstd.samr.gov.cn",
  "hierarchy": "国家标准",
  "chapters": [
    {
      "chapter_title": "1 总则",
      "articles": [
        {
          "article_no": "1.0.1",
          "content": "为了预防建筑火灾，减少火灾危害，保护人身和财产安全，制定本规范。"
        },
        {
          "article_no": "1.0.2",
          "content": "本规范适用于下列新建、扩建和改建的建筑：1 厂房；2 仓库；3 民用建筑..."
        }
      ]
    },
    {
      "chapter_title": "2 术语",
      "articles": [
        {
          "article_no": "2.1.1",
          "content": "高层建筑 high-rise building：建筑高度大于27m的住宅建筑和建筑高度大于24m的非单层厂房、仓库和其他民用建筑。"
        }
      ]
    }
  ],
  "full_text": "建筑设计防火规范 GB 50016-2014\n\n1 总则\n\n1.0.1 为了预防建筑火灾...",
  "crawl_time": "2026-06-12T09:00:00.000Z",
  "content_hash": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
  "revisions": [],
  "keywords": ["防火", "建筑设计", "消防", "耐火等级", "防火间距"],
  "tags": ["建筑防火", "消防设计审查", "消防验收"]
}
```

---

## 4. 数据质量约束

| 约束 | 规则 | 处理方式 |
|------|------|---------|
| 唯一性 | `id` 全局唯一 | 用来源 ID + 哈希兜底 |
| 完整性 | `title`、`full_text`、`publish_date` 不为空 | 空值采集需标记 status 为 unknown |
| 去重 | `content_hash` 相同视为同一文档 | 多源采集时比较 hash，相同则合并来源 |
| 版本追踪 | `revisions` 记录变更历史 | 重新采集时对比 `content_hash`，变化则追加 revision |
| 溯源 | `source_url` 指向原始网页 | 必须为最终详情页 URL，不能是中间页 |

---

## 5. Chunk 切分建议（供后续向量化阶段参考）

| 文档类型 | 推荐 Chunk 粒度 | 元数据携带 |
|---------|----------------|-----------|
| 法律/行政法规 | 按"条" (article) | `hierarchy` > `chapter_title` > `article_no` |
| 部门规章 | 按"条" 或"款" (小段落) | 同上 |
| 国家标准 | 按小节编号 (如 `3.1`, `3.1.2`) | `hierarchy` > `chapter_title` > `article_no` |
| 地方性法规 | 按"条" | 同上 + `publisher`（省份） |
| 政策文件/解读 | 按自然段落（500-1500 字） | 全文标题 + 段落序号 |

每个 Chunk 的元数据包含完整引用链，使检索结果可追溯到原始条款。示例：

```json
{
  "chunk_id": "e8a7b3c9...-ch001",
  "doc_id": "e8a7b3c9d1f4a2b6",
  "doc_title": "中华人民共和国消防法",
  "hierarchy": "法律",
  "chapter_title": "第一章 总则",
  "article_no": "第一条",
  "chunk_index": 0,
  "content": "为了预防火灾和减少火灾危害，加强应急救援工作，保护人身、财产安全，维护公共安全，制定本法。"
}
```

---

> Schema 版本 v1.0。如 PRD 有更新或业务需求变化，需同步更新本文档。
