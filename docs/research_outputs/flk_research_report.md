# 国家法律法规数据库 (flk.npc.gov.cn) 调研报告

> 调研时间：2026-06-12
> 调研目标：分析站内检索机制，为爬虫开发提供技术依据

## 一、网站概述

- 网站名称：国家法律法规数据库
- 官方地址：https://flk.npc.gov.cn
- 主管单位：全国人大常委会办公厅
- 技术架构：Vue.js SPA（单页应用），后端 Nginx
- 文档存储：Word (DOCX)、PDF 格式，通过独立文件服务器分发

## 二、站内检索机制

### 2.1 API 架构

网站采用前后端分离架构，前端 Vue.js SPA 通过 AJAX 请求后端 API 获取数据。

| 端点 | 方法 | 用途 |
|------|------|------|
| `https://flk.npc.gov.cn/api/` | GET | 法律法规列表检索 |
| `https://flk.npc.gov.cn/api/detail` | POST | 获取单条法规详情（含文件下载路径） |
| `https://wb.flk.npc.gov.cn/{path}` | GET | 下载法规文件（WORD/PDF） |

### 2.2 检索接口参数

**列表检索 (GET /api/)**

| 参数 | 类型 | 说明 | 示例值 |
|------|------|------|--------|
| `type` | string | 法规类型过滤 | `flfg`(法律), `xzfg`(行政法规), `dfxfg`(地方性法规), `sfjs`(司法解释), `jcfg`(监察法规), 空=全部 |
| `searchType` | string | 检索范围与模式 | `title;vague`(标题模糊), `title;exact`(标题精确), `body;vague`(正文模糊) |
| `searchWord` | string | 搜索关键词 | `消防` |
| `sortTr` | string | 排序字段与方向 | `f_bbrq_s;desc`(发布日期降序), `f_bbrq_s;asc`(发布日期升序) |
| `sort` | string | 是否启用排序 | `true` / `false` |
| `gbrqStart` | string | 公布日期起始 | `2020-01-01` |
| `gbrqEnd` | string | 公布日期截止 | `2024-12-31` |
| `sxrqStart` | string | 施行日期起始 | `2020-01-01` |
| `sxrqEnd` | string | 施行日期截止 | `2024-12-31` |
| `page` | int | 页码（从 1 开始） | `1` |
| `size` | int | 每页条数 | `10`（最大 100） |
| `_` | int | 时间戳（毫秒） | `1693416204292` |

### 2.3 请求头要求

关键请求头（缺失可能导致被 WAF 拦截）：

```
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ...
Accept: application/json, text/javascript, */*; q=0.01
X-Requested-With: XMLHttpRequest
Referer: https://flk.npc.gov.cn/index.html
```

### 2.4 返回格式

**列表接口返回：**

```json
{
  "success": true,
  "result": {
    "totalSizes": 22556,
    "page": 1,
    "size": 10,
    "data": [
      {
        "id": "ZmY4MDgwODE2ZjNjYmIzYzAxNmY0MGZkM2JiZjEwMjQ%3D",
        "title": "中华人民共和国消防法",
        "office": "全国人大常委会",
        "publish": "2021-04-29 00:00:00",
        "expiry": "",
        "type": "法律",
        "status": "有效"
      }
    ]
  }
}
```

**详情接口返回：**

```json
{
  "success": true,
  "result": {
    "title": "中华人民共和国消防法",
    "office": "全国人大常委会",
    "publish": "2021-04-29",
    "body": [
      {
        "type": "WORD",
        "path": "/flfg/WORD/be0c5465e9e24c8fb2c4b7e3f7e24d95.docx"
      },
      {
        "type": "PDF",
        "path": "/flfg/PDF/be0c5465e9e24c8fb2c4b7e3f7e24d95.pdf"
      },
      {
        "type": "HTML",
        "path": "/flfg/HTML/be0c5465e9e24c8fb2c4b7e3f7e24d95.html"
      }
    ]
  }
}
```

文件下载 URL 拼接规则：`https://wb.flk.npc.gov.cn` + `path`

### 2.5 会话与频率限制

1. **WAF 防护**：网站使用阿里云 WAF（Web Application Firewall），需携带 `acw_tc` Cookie
2. **Cookie 机制**：访问主页后 WAF 通过 JavaScript 挑战设置 `acw_tc`，后续 API 请求需携带此 Cookie
3. **频率限制**：建议请求间隔 1-3 秒，同一 IP 短时间大量请求可能触发限流
4. **无需登录**：基本检索和下载不需要用户登录

### 2.6 法规类型代码

| 代码 | 类型 |
|------|------|
| `flfg` | 法律（含宪法及相关法、民商法、行政法、经济法、社会法、刑法、诉讼与非诉讼程序法） |
| `xzfg` | 行政法规 |
| `dfxfg` | 地方性法规 |
| `sfjs` | 司法解释 |
| `jcfg` | 监察法规 |

## 三、数据质量评估

### 3.1 正文格式

法规正文以 DOCX 格式为主，结构清晰：
- 标题、制定机关、发布日期等元数据
- 章节标题（第X章 XXX）
- 条款编号（第X条）
- 附录、附则

### 3.2 数据完整性

- 收录法律法规总数：约 22,000+ 条（截至 2023 年 9 月）
- 覆盖范围：宪法、法律、行政法规、地方性法规、司法解释、监察法规
- 法律状态标注完整：有效、已修改、已废止
- 文档格式：WORD 为主，部分有 PDF

### 3.3 文本提取方案

DOCX 文件可通过 `python-docx` 库解析，提取纯文本并保留段落结构。条款编号（第X条）可作为结构化切分的锚点。

## 四、爬取策略建议

1. **API 直连模式**（优先）：携带正确请求头直接调用 API，效率最高
2. **浏览器自动化模式**（备选）：当 API 被 WAF 拦截时，使用 Selenium/Playwright 模拟浏览器操作
3. **混合模式**：先尝试 API 直连，失败后自动降级为浏览器自动化

### 访问注意事项

- 直接 HTTP 请求可能在无 `acw_tc` Cookie 时被 WAF 拦截，返回 SPA 首页 HTML 而非 JSON
- 首次访问需通过浏览器获取 WAF Cookie
- 设置合理的时间间隔（1-2 秒），避免触发频率限制
