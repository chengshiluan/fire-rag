#!/usr/bin/env python3
"""
通用地方性法规爬虫框架 (General-Purpose Local Regulation Crawler Framework)

支持通过配置文件定义 URL 模板、CSS 选择器和分页参数，
从各省市政府网站抓取地方性消防法规，输出为结构化 JSON。

设计原则：
- 可配置：所有采集规则通过 YAML/JSON 配置文件定义
- 可扩展：支持多站点、多页面模板的并发采集
- 鲁棒性：内置重试、延时、User-Agent 轮换等反爬对抗机制
- 结构化输出：JSON 格式，含元数据（来源、采集时间、法规名称等）和正文

参考：
- LawRefBook/Laws (GitHub): flk.npc.gov.cn API 交互
- Yuhamixli/Law-Crawler-RPA-RAG-MCP (GitHub): 多策略采集调度
"""

import json
import logging
import re
import sys
import time
import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("local_reg_crawler")


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class RegulationEntry:
    """单条法规的结构化记录"""

    # 元数据
    source_name: str                          # 数据来源（如 "北京市人民政府-首都之窗"）
    source_url: str                           # 来源 URL
    regulation_title: str                     # 法规名称
    regulation_type: str = ""                 # 法规类型（地方性法规/政府规章/规范性文件）
    document_number: str = ""                 # 文号
    issuing_authority: str = ""               # 制定机关
    publish_date: str = ""                    # 发布日期
    effective_date: str = ""                  # 施行日期
    status: str = "现行有效"                   # 效力状态

    # 正文
    full_text: str = ""                       # 法规全文
    text_sections: list[dict] = field(default_factory=list)  # 结构化章节 [{type, title, content, articles}]

    # 采集信息
    crawl_timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    content_hash: str = ""                    # 正文 MD5（用于去重和增量更新检测）
    raw_html: str = ""                        # 原始 HTML（可选保留用于审计）

    def compute_hash(self) -> str:
        if self.full_text:
            self.content_hash = hashlib.md5(self.full_text.encode("utf-8")).hexdigest()
        return self.content_hash


# ---------------------------------------------------------------------------
# Fetch Layer — HTTP Client with Retry & Anti-Rate-Limit
# ---------------------------------------------------------------------------

class FetchClient:
    """HTTP 客户端，内置重试、延时、UA 轮换"""

    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    ]

    def __init__(self, request_delay: float = 1.0, max_retries: int = 3, timeout: int = 30):
        self.request_delay = request_delay
        self.max_retries = max_retries
        self.timeout = timeout
        self.session = requests.Session()
        self._last_request_time = 0.0
        self._ua_index = 0

    def _get_headers(self) -> dict:
        ua = self.USER_AGENTS[self._ua_index % len(self.USER_AGENTS)]
        self._ua_index += 1
        return {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }

    def _respect_delay(self):
        elapsed = time.time() - self._last_request_time
        if elapsed < self.request_delay:
            time.sleep(self.request_delay - elapsed)

    def get(self, url: str, params: dict | None = None) -> requests.Response | None:
        for attempt in range(1, self.max_retries + 1):
            try:
                self._respect_delay()
                resp = self.session.get(
                    url, params=params, headers=self._get_headers(),
                    timeout=self.timeout,
                )
                self._last_request_time = time.time()
                resp.raise_for_status()
                # 检测编码
                if resp.encoding and resp.encoding.lower() in ("iso-8859-1", "latin-1"):
                    # 尝试从内容推断（中文站点常见问题）
                    match = re.search(rb'charset=["\']?([\w-]+)', resp.content[:2048])
                    if match:
                        resp.encoding = match.group(1).decode("ascii")
                    else:
                        resp.encoding = "utf-8"
                return resp
            except requests.RequestException as e:
                logger.warning(f"Request attempt {attempt}/{self.max_retries} failed for {url}: {e}")
                if attempt < self.max_retries:
                    time.sleep(2 ** attempt)
        logger.error(f"All {self.max_retries} attempts failed for {url}")
        return None


# ---------------------------------------------------------------------------
# Parse Layer — CSS Selector-based Extraction
# ---------------------------------------------------------------------------

class RegulationParser:
    """基于 CSS 选择器的法规页面解析器"""

    # 常用法规正文行模式（用于分章分节识别）
    PART_PATTERN = re.compile(r"^第[一二三四五六七八九十百千0-9]+编\s*")
    CHAPTER_PATTERN = re.compile(r"^第[一二三四五六七八九十百千0-9]+章\s*.*")
    SECTION_PATTERN = re.compile(r"^第[一二三四五六七八九十百千0-9]+节\s*.*")
    ARTICLE_PATTERN = re.compile(r"^第[一二三四五六七八九十百千0-9]+条\s*")

    @staticmethod
    def safe_select_one(soup: BeautifulSoup, selector: str) -> Optional[Tag]:
        """安全选择单个元素"""
        return soup.select_one(selector)

    @staticmethod
    def safe_select_all(soup: BeautifulSoup, selector: str) -> list[Tag]:
        """安全选择多个元素"""
        return soup.select(selector)

    @staticmethod
    def extract_text(soup: BeautifulSoup, selector: str, default: str = "") -> str:
        """提取单个选择器的文本"""
        el = soup.select_one(selector)
        if el:
            return el.get_text(strip=True)
        return default

    @staticmethod
    def extract_meta_from_table(soup: BeautifulSoup, meta_selectors: dict[str, str]) -> dict[str, str]:
        """从表格/元数据区域提取法规元数据"""
        meta = {}
        for key, selector in meta_selectors.items():
            meta[key] = RegulationParser.extract_text(soup, selector)
        return meta

    @staticmethod
    def extract_full_text(soup: BeautifulSoup, content_selector: str) -> str:
        """提取法规正文"""
        el = soup.select_one(content_selector)
        if el:
            return el.get_text(separator="\n", strip=True)
        return ""

    @staticmethod
    def parse_articles(full_text: str) -> list[dict]:
        """将法规正文解析为结构化条目（编->章->节->条）"""
        lines = full_text.split("\n")
        sections: list[dict] = []
        current_section: dict = {"type": "preamble", "title": "序言", "content": [], "articles": []}
        current_article: dict = {"number": "", "content": []}

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # 编
            if RegulationParser.PART_PATTERN.match(line):
                if current_section["content"] or current_section["articles"]:
                    sections.append(current_section)
                current_section = {"type": "part", "title": line, "content": [], "articles": []}
                continue

            # 章
            if RegulationParser.CHAPTER_PATTERN.match(line):
                if current_section["content"] or current_section["articles"]:
                    sections.append(current_section)
                current_section = {"type": "chapter", "title": line, "content": [], "articles": []}
                continue

            # 节
            if RegulationParser.SECTION_PATTERN.match(line):
                current_section = {"type": "section", "title": line, "content": [], "articles": []}
                continue

            # 条
            if RegulationParser.ARTICLE_PATTERN.match(line):
                if current_article["content"]:
                    current_section["articles"].append(current_article)
                current_article = {"number": line.split("　")[0].split(" ")[0], "content": [line]}
                continue

            # 普通行
            if current_article["number"]:
                current_article["content"].append(line)
            else:
                current_section["content"].append(line)

        # 收尾
        if current_article["content"]:
            current_section["articles"].append(current_article)
        if current_section["content"] or current_section["articles"]:
            sections.append(current_section)

        return sections


# ---------------------------------------------------------------------------
# Page Templates — 为每个站点定义选择器规则
# ---------------------------------------------------------------------------

@dataclass
class ListPageTemplate:
    """列表页模板：定义如何从列表页提取法规条目链接"""
    name: str                                    # 模板名称
    base_url: str                                # 基础 URL
    list_url_template: str                       # 列表页 URL 模板（支持 {page} 占位符）
    item_selector: str                           # 法规条目 CSS 选择器
    title_selector: str                          # 标题 CSS 选择器（相对于 item）
    link_selector: str                           # 链接 CSS 选择器（相对于 item）
    date_selector: str = ""                      # 日期 CSS 选择器（可选）
    pagination_param: str = "page"               # 分页参数名
    page_start: int = 1                          # 起始页码
    page_end: int = 1                            # 结束页码（-1 表示自动探测）
    encoding: str = "utf-8"


@dataclass
class DetailPageTemplate:
    """详情页模板：定义如何从法规详情页提取正文和元数据"""
    name: str                                    # 模板名称
    content_selector: str                        # 正文 CSS 选择器
    title_selector: str = "h1"                   # 标题选择器
    publish_date_selector: str = ""              # 发布日期选择器
    issuing_authority_selector: str = ""         # 制定机关选择器
    document_number_selector: str = ""           # 文号选择器
    additional_meta_selectors: dict[str, str] = field(default_factory=dict)
    exclude_selectors: list[str] = field(default_factory=list)  # 需排除的子元素
    encoding: str = "utf-8"


# ---------------------------------------------------------------------------
# Site Configuration — 绑定列表页 + 详情页模板
# ---------------------------------------------------------------------------

@dataclass
class SiteConfig:
    """单个站点的完整采集配置"""
    site_id: str                                 # 站点标识
    source_name: str                             # 来源名称（用于输出）
    list_template: ListPageTemplate
    detail_template: DetailPageTemplate
    regulation_type: str = "地方性法规"            # 默认法规类型
    enabled: bool = True


# ---------------------------------------------------------------------------
# Pre-built Site Configurations
# ---------------------------------------------------------------------------

BUILTIN_SITES: list[SiteConfig] = [
    # ------------------------------------------------------------------
    # 北京
    # ------------------------------------------------------------------
    SiteConfig(
        site_id="beijing_dfxfg",
        source_name="北京市人民政府-首都之窗-地方性法规",
        regulation_type="地方性法规",
        list_template=ListPageTemplate(
            name="beijing_list",
            base_url="https://www.beijing.gov.cn",
            list_url_template="https://www.beijing.gov.cn/zhengce/dfxfg/",
            item_selector=".news-list li, .list-box li, .listContent li",
            title_selector="a",
            link_selector="a",
            date_selector="span.date, .time",
            page_start=1,
            page_end=-1,
        ),
        detail_template=DetailPageTemplate(
            name="beijing_detail",
            content_selector=".article-content, .main-content, #mainText, .text-content",
            title_selector="h1, .article-title",
            publish_date_selector=".article-date, .info-date, .time",
            exclude_selectors=[".print-btn", ".share", ".related-links"],
        ),
    ),
    # ------------------------------------------------------------------
    # 上海 - 行政规范性文件数据库（消防相关）
    # ------------------------------------------------------------------
    SiteConfig(
        site_id="shanghai_fire_xzgfx",
        source_name="上海市行政规范性文件数据库",
        regulation_type="规范性文件",
        list_template=ListPageTemplate(
            name="shanghai_list",
            base_url="http://service.shanghai.gov.cn",
            list_url_template="http://service.shanghai.gov.cn/xingzhengwendangkujyh/XZGFList.aspx?departid1=CITY_81",
            item_selector="table tr:has(td)",
            title_selector="td:first-child a, td:nth-child(1) a",
            link_selector="td:first-child a, td:nth-child(1) a",
            date_selector="td:nth-child(3)",
            page_start=1,
            page_end=1,  # ASPX 可能使用 POST 分页而非 GET 参数
            encoding="utf-8",
        ),
        detail_template=DetailPageTemplate(
            name="shanghai_detail",
            content_selector=".content, #content, .main-content, .article-content",
            title_selector="h1, .title",
            publish_date_selector=".date, .time, .info-date",
            document_number_selector=".document-number, .wenhao",
        ),
    ),
    # ------------------------------------------------------------------
    # 广东
    # ------------------------------------------------------------------
    SiteConfig(
        site_id="guangdong_zcfgk",
        source_name="广东省人民政府-政策法规库",
        regulation_type="地方性法规",
        list_template=ListPageTemplate(
            name="guangdong_list",
            base_url="https://www.gd.gov.cn",
            list_url_template="https://www.gd.gov.cn/zwgk/wjk/zcfgk/index_{page}.html",
            item_selector=".list-item, .news-list li, .viewList li, .data-list li",
            title_selector="a",
            link_selector="a",
            date_selector="span.date, .time",
            page_start=1,
            page_end=-1,
        ),
        detail_template=DetailPageTemplate(
            name="guangdong_detail",
            content_selector="#content, .article-content, .main-content, .text-content",
            title_selector="h1, .article-title",
            publish_date_selector=".article-date, .info-date, .time",
            issuing_authority_selector=".authority, .source",
        ),
    ),
    # ------------------------------------------------------------------
    # 广东消防总队
    # ------------------------------------------------------------------
    SiteConfig(
        site_id="guangdong_119",
        source_name="广东省消防救援总队",
        regulation_type="地方性法规",
        list_template=ListPageTemplate(
            name="gd119_list",
            base_url="https://gd.119.gov.cn",
            list_url_template="https://gd.119.gov.cn/zwgk/flfg/",
            item_selector=".news-list li, .list-box li, .list-content li, .list-item",
            title_selector="a",
            link_selector="a",
            date_selector="span.date, .time",
            page_start=1,
            page_end=-1,
        ),
        detail_template=DetailPageTemplate(
            name="gd119_detail",
            content_selector=".article-content, .content, .main-text",
            title_selector="h1, .article-title, .title",
            publish_date_selector=".date, .time, .info-date",
            issuing_authority_selector=".authority, .source",
        ),
    ),
    # ------------------------------------------------------------------
    # 江苏消防总队
    # ------------------------------------------------------------------
    SiteConfig(
        site_id="jiangsu_119",
        source_name="江苏省消防救援总队",
        regulation_type="地方性法规",
        list_template=ListPageTemplate(
            name="jiangsu_119_list",
            base_url="https://js.119.gov.cn",
            list_url_template="https://js.119.gov.cn/zwgk/flfg/",
            item_selector=".news-list li, .list-box li, .list-item",
            title_selector="a",
            link_selector="a",
            date_selector="span.date, .time",
            page_start=1,
            page_end=-1,
        ),
        detail_template=DetailPageTemplate(
            name="jiangsu_119_detail",
            content_selector=".article-content, .content, .main-text",
            title_selector="h1, .article-title",
            publish_date_selector=".date, .time",
        ),
    ),
    # ------------------------------------------------------------------
    # 浙江省 - 预留模板（URL 待确认）
    # ------------------------------------------------------------------
    SiteConfig(
        site_id="zhejiang",
        source_name="浙江省人民政府",
        regulation_type="地方性法规",
        enabled=False,  # URL 待确认，暂不启用
        list_template=ListPageTemplate(
            name="zhejiang_list",
            base_url="https://www.zj.gov.cn",
            list_url_template="https://www.zj.gov.cn/col/col1229012345/index.html",
            item_selector=".list-item a, .news-list li a",
            title_selector="a",
            link_selector="a",
            date_selector="span.date",
            page_start=1,
            page_end=-1,
        ),
        detail_template=DetailPageTemplate(
            name="zhejiang_detail",
            content_selector=".article-content, .content",
            title_selector="h1",
        ),
    ),
    # ------------------------------------------------------------------
    # 山东省 - 预留模板（URL 待确认）
    # ------------------------------------------------------------------
    SiteConfig(
        site_id="shandong",
        source_name="山东省人民政府",
        regulation_type="地方性法规",
        enabled=False,
        list_template=ListPageTemplate(
            name="shandong_list",
            base_url="http://www.shandong.gov.cn",
            list_url_template="http://www.shandong.gov.cn/col/col92364/index.html",
            item_selector=".list-item a, .news-list li a",
            title_selector="a",
            link_selector="a",
            date_selector="span.date",
            page_start=1,
            page_end=-1,
        ),
        detail_template=DetailPageTemplate(
            name="shandong_detail",
            content_selector=".article-content, .content",
            title_selector="h1",
        ),
    ),
]


# ---------------------------------------------------------------------------
# Crawler Engine
# ---------------------------------------------------------------------------

class RegulationCrawler:
    """法规爬虫主引擎"""

    def __init__(
        self,
        output_dir: str = "./crawled_regulations",
        request_delay: float = 1.0,
        max_retries: int = 3,
        sites: list[SiteConfig] | None = None,
    ):
        self.client = FetchClient(request_delay=request_delay, max_retries=max_retries)
        self.parser = RegulationParser()
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.sites = sites or [s for s in BUILTIN_SITES if s.enabled]
        self.results: list[RegulationEntry] = []
        self._existing_hashes: set[str] = set()

    def _load_existing_hashes(self):
        """加载已有数据的哈希，用于增量去重"""
        for json_path in self.output_dir.glob("*.json"):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for entry in data if isinstance(data, list) else [data]:
                    if "content_hash" in entry and entry["content_hash"]:
                        self._existing_hashes.add(entry["content_hash"])
            except Exception:
                pass

    def _discover_detail_urls(self, site: SiteConfig) -> list[dict]:
        """从列表页发现法规详情页 URL"""
        template = site.list_template
        discovered: list[dict] = []

        # 单页或分页
        pages = [template.list_url_template]
        if "{page}" in template.list_url_template:
            pages = [template.list_url_template.format(page=p) for p in range(
                template.page_start, template.page_end + 1 if template.page_end > 0 else 10
            )]

        for page_url in pages:
            logger.info(f"Discovering from {page_url}")
            resp = self.client.get(page_url)
            if not resp:
                continue
            resp.encoding = template.encoding
            soup = BeautifulSoup(resp.text, "html.parser")
            items = self.parser.safe_select_all(soup, template.item_selector)

            for item in items:
                title_el = self.parser.safe_select_one(item, template.title_selector)
                link_el = self.parser.safe_select_one(item, template.link_selector)
                if not title_el or not link_el:
                    continue
                title = title_el.get_text(strip=True)
                href = link_el.get("href", "")
                if not href:
                    continue
                detail_url = urljoin(template.base_url, href)
                # 跳过非本站链接
                if urlparse(detail_url).netloc not in urlparse(template.base_url).netloc:
                    if not detail_url.startswith("http"):
                        continue

                date_str = ""
                if template.date_selector:
                    date_el = self.parser.safe_select_one(item, template.date_selector)
                    if date_el:
                        date_str = date_el.get_text(strip=True)

                discovered.append({
                    "title": title,
                    "url": detail_url,
                    "date": date_str,
                })

            # 检查是否还有下一页（简单探测：检查"下一页"链接是否存在）
            # 注：精确的分页探测需要根据站点定制
            if template.page_end == -1 and len(discovered) > 0:
                next_link = soup.select_one('a:contains("下一页"), a.next, .pagination .next')
                if not next_link:
                    break

        # 去重
        seen: set[str] = set()
        unique: list[dict] = []
        for item in discovered:
            if item["url"] not in seen:
                seen.add(item["url"])
                unique.append(item)
        return unique

    def _crawl_detail_page(self, url: str, site: SiteConfig) -> Optional[RegulationEntry]:
        """抓取并解析单个法规详情页"""
        template = site.detail_template
        resp = self.client.get(url)
        if not resp:
            return None
        resp.encoding = template.encoding
        soup = BeautifulSoup(resp.text, "html.parser")

        # 排除不需要的子元素
        for sel in template.exclude_selectors:
            for el in soup.select(sel):
                el.decompose()

        # 提取元数据
        title = self.parser.extract_text(soup, template.title_selector)
        publish_date = self.parser.extract_text(soup, template.publish_date_selector)
        issuing_authority = self.parser.extract_text(soup, template.issuing_authority_selector)
        document_number = self.parser.extract_text(soup, template.document_number_selector)

        # 额外元数据字段
        additional_meta = self.parser.extract_meta_from_table(soup, template.additional_meta_selectors)

        # 提取正文
        full_text = self.parser.extract_full_text(soup, template.content_selector)

        if not title and not full_text:
            logger.warning(f"No content extracted from {url}")
            return None

        # 构建条目
        entry = RegulationEntry(
            source_name=site.source_name,
            source_url=url,
            regulation_title=title,
            regulation_type=site.regulation_type,
            document_number=document_number or additional_meta.get("document_number", ""),
            issuing_authority=issuing_authority,
            publish_date=publish_date,
            effective_date=additional_meta.get("effective_date", ""),
            status=additional_meta.get("status", "现行有效"),
            full_text=full_text,
            text_sections=self.parser.parse_articles(full_text),
            raw_html=resp.text,
        )
        entry.compute_hash()
        return entry

    def run(self, keyword_filter: str = "") -> list[RegulationEntry]:
        """执行完整采集流程

        Args:
            keyword_filter: 如果指定，仅处理标题包含该关键词的法规
        """
        self._load_existing_hashes()
        all_results: list[RegulationEntry] = []

        for site in self.sites:
            logger.info(f"=== Processing site: {site.source_name} ===")
            detail_urls = self._discover_detail_urls(site)

            # 按关键词过滤（在列表页层面）
            if keyword_filter:
                detail_urls = [d for d in detail_urls if keyword_filter in d.get("title", "")]
            logger.info(f"Found {len(detail_urls)} potential regulation pages on this site")

            for idx, item in enumerate(detail_urls):
                logger.info(f"[{idx+1}/{len(detail_urls)}] Fetching: {item['title'][:60]}...")
                entry = self._crawl_detail_page(item["url"], site)
                if entry is None:
                    continue

                # 去重
                if entry.content_hash in self._existing_hashes:
                    logger.debug(f"Duplicate skipped: {entry.regulation_title}")
                    continue

                all_results.append(entry)
                self._existing_hashes.add(entry.content_hash)

        self.results.extend(all_results)
        return all_results

    def save_results(self, filename: str = "local_regulations.json") -> str:
        """保存结果到 JSON 文件"""
        output_path = self.output_dir / filename
        data = [asdict(r) for r in self.results]
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"Saved {len(data)} regulations to {output_path}")
        return str(output_path)

    def export_summary(self, filename: str = "local_regulations_summary.json") -> str:
        """导出简要台账（不含正文和原始HTML，适合快速浏览）"""
        output_path = self.output_dir / filename
        summary = []
        for r in self.results:
            summary.append({
                "regulation_title": r.regulation_title,
                "regulation_type": r.regulation_type,
                "document_number": r.document_number,
                "issuing_authority": r.issuing_authority,
                "publish_date": r.publish_date,
                "effective_date": r.effective_date,
                "status": r.status,
                "source_name": r.source_name,
                "source_url": r.source_url,
                "content_hash": r.content_hash,
                "crawl_timestamp": r.crawl_timestamp,
            })
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        logger.info(f"Saved {len(summary)} summaries to {output_path}")
        return str(output_path)


# ---------------------------------------------------------------------------
# Site Config Loader — 从外部 JSON 文件加载站点配置
# ---------------------------------------------------------------------------

def load_sites_from_json(config_path: str) -> list[SiteConfig]:
    """从 JSON 配置文件加载站点采集规则

    配置文件格式示例 (sites_config.json):
    {
      "sites": [
        {
          "site_id": "my_site",
          "source_name": "我的数据源",
          "regulation_type": "地方性法规",
          "enabled": true,
          "list_template": {
            "name": "my_list",
            "base_url": "https://example.com",
            "list_url_template": "https://example.com/laws/page/{page}.html",
            "item_selector": ".law-list li",
            "title_selector": "a.title",
            "link_selector": "a",
            "date_selector": ".date",
            "page_start": 1,
            "page_end": 10
          },
          "detail_template": {
            "name": "my_detail",
            "content_selector": ".law-content",
            "title_selector": "h1",
            "publish_date_selector": ".pub-date",
            "issuing_authority_selector": ".authority",
            "document_number_selector": ".doc-number"
          }
        }
      ]
    }
    """
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    sites: list[SiteConfig] = []
    for site_data in data.get("sites", []):
        list_data = site_data["list_template"]
        detail_data = site_data["detail_template"]

        list_template = ListPageTemplate(
            name=list_data["name"],
            base_url=list_data["base_url"],
            list_url_template=list_data["list_url_template"],
            item_selector=list_data["item_selector"],
            title_selector=list_data["title_selector"],
            link_selector=list_data["link_selector"],
            date_selector=list_data.get("date_selector", ""),
            pagination_param=list_data.get("pagination_param", "page"),
            page_start=list_data.get("page_start", 1),
            page_end=list_data.get("page_end", 1),
            encoding=list_data.get("encoding", "utf-8"),
        )
        detail_template = DetailPageTemplate(
            name=detail_data["name"],
            content_selector=detail_data["content_selector"],
            title_selector=detail_data.get("title_selector", "h1"),
            publish_date_selector=detail_data.get("publish_date_selector", ""),
            issuing_authority_selector=detail_data.get("issuing_authority_selector", ""),
            document_number_selector=detail_data.get("document_number_selector", ""),
            additional_meta_selectors=detail_data.get("additional_meta_selectors", {}),
            exclude_selectors=detail_data.get("exclude_selectors", []),
            encoding=detail_data.get("encoding", "utf-8"),
        )
        site = SiteConfig(
            site_id=site_data["site_id"],
            source_name=site_data["source_name"],
            list_template=list_template,
            detail_template=detail_template,
            regulation_type=site_data.get("regulation_type", "地方性法规"),
            enabled=site_data.get("enabled", True),
        )
        sites.append(site)

    return sites


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="通用地方性法规爬虫框架",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # 使用内置站点配置，抓取消防相关法规
  python crawler_local_framework.py --keyword 消防

  # 使用外部配置文件
  python crawler_local_framework.py --config sites_config.json --keyword 消防

  # 导出简要台账
  python crawler_local_framework.py --keyword 消防 --summary-only
        """,
    )
    parser.add_argument("--config", "-c", type=str, help="外部站点配置文件 (JSON)")
    parser.add_argument("--keyword", "-k", type=str, default="消防", help="标题关键词过滤 (默认: 消防)")
    parser.add_argument("--output-dir", "-o", type=str, default="./crawled_regulations", help="输出目录")
    parser.add_argument("--delay", "-d", type=float, default=1.0, help="请求间隔 (秒)")
    parser.add_argument("--retries", "-r", type=int, default=3, help="最大重试次数")
    parser.add_argument("--summary-only", action="store_true", help="仅导出简要台账")
    parser.add_argument("--sites", "-s", nargs="*", help="指定运行的 site_id (默认: 全部启用的站点)")

    args = parser.parse_args()

    # 加载站点配置
    if args.config:
        sites = load_sites_from_json(args.config)
        logger.info(f"Loaded {len(sites)} sites from config file")
    else:
        sites = [s for s in BUILTIN_SITES if s.enabled]
        logger.info(f"Using {len(sites)} built-in site configurations")

    # 按需过滤
    if args.sites:
        sites = [s for s in sites if s.site_id in args.sites]
        logger.info(f"Filtered to {len(sites)} sites: {[s.site_id for s in sites]}")

    if not sites:
        logger.error("No enabled sites to crawl. Check your configuration.")
        sys.exit(1)

    # 执行采集
    crawler = RegulationCrawler(
        output_dir=args.output_dir,
        request_delay=args.delay,
        max_retries=args.retries,
        sites=sites,
    )

    results = crawler.run(keyword_filter=args.keyword)

    # 保存结果
    if results:
        summary_path = crawler.export_summary()
        logger.info(f"Summary exported to: {summary_path}")
        if not args.summary_only:
            full_path = crawler.save_results()
            logger.info(f"Full results exported to: {full_path}")
    else:
        logger.warning("No results found. Check site configurations and network connectivity.")


if __name__ == "__main__":
    main()
