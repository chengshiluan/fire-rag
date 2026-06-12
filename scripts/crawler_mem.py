#!/usr/bin/env python3
"""
crawler_mem.py — 应急管理部 (mem.gov.cn) 政策法规爬虫

从 mem.gov.cn 政策法规相关栏目采集消防相关文件：
- 部令 / 规章详情页 (HTML 正文)
- 通知公告详情页 (HTML 正文)
- PDF 附件下载与文本提取

输出 JSON 格式，每条记录包含：标题、发布日期、正文内容、来源 URL、PDF 路径。

用法:
  # 基础用法：爬取部令 + 通知公告，筛选消防相关
  python crawler_mem.py

  # 指定栏目
  python crawler_mem.py --sections bl,tz,yjbgg

  # 测试模式：只爬前 5 页
  python crawler_mem.py --max-pages 5

  # 断点续传
  python crawler_mem.py --resume

  # 只采集不提取 PDF 文本（仅保存 HTML 正文）
  python crawler_mem.py --no-pdf

  # 详细输出
  python crawler_mem.py --verbose

依赖:
  pip install requests beautifulsoup4 pdfplumber pymupdf lxml
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime, date as date_type
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# 常量与配置
# ---------------------------------------------------------------------------

BASE_URL = "https://www.mem.gov.cn"
REQUEST_TIMEOUT = 30  # 秒
DEFAULT_DELAY = 2.0  # 请求间隔（秒）
MAX_RETRIES = 3
RETRY_BACKOFF = 2.0  # 重试退避因子

# 数据目录
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "industry_guidelines"
PDF_DIR = DATA_DIR / "pdf"
CHECKPOINT_FILE = DATA_DIR / "checkpoint_mem.json"
OUTPUT_FILE = DATA_DIR / "mem_fire_documents.json"

# 消防相关关键词（用于标题筛选）
FIRE_KEYWORDS = [
    "消防", "防火", "灭火", "救援", "应急", "火灾",
    "森林防火", "草原防火", "高层建筑", "易燃", "危化品",
    "矿山安全", "烟花爆竹", "消防技术", "消防安全",
    "消防救援", "消防设施", "消防产品", "消防队伍",
    "消防员", "消防车", "消防通道", "防火门",
    "自动喷水", "火灾报警", "灭火器", "消防给水",
    "消火栓", "防排烟", "疏散", "避难层",
]

# mem.gov.cn 政策法规栏目配置
# 每个 section 包含: 名称, 列表页 URL, 详情页选择器
SECTIONS = {
    "bl": {
        "name": "部令",
        "list_url": f"{BASE_URL}/gk/tzgg/bl/",
        "description": "应急管理部令（部门规章）",
    },
    "tz": {
        "name": "通知",
        "list_url": f"{BASE_URL}/gk/tzgg/tz/",
        "description": "通知公告 - 通知类",
    },
    "yjbgg": {
        "name": "公告",
        "list_url": f"{BASE_URL}/gk/tzgg/yjbgg/",
        "description": "通知公告 - 公告类",
    },
    "yj": {
        "name": "意见",
        "list_url": f"{BASE_URL}/gk/tzgg/yj/",
        "description": "通知公告 - 意见类",
    },
    "h": {
        "name": "函",
        "list_url": f"{BASE_URL}/gk/tzgg/h/",
        "description": "通知公告 - 函件类",
    },
    "tb": {
        "name": "通报",
        "list_url": f"{BASE_URL}/gk/tzgg/tb/",
        "description": "通知公告 - 通报类",
    },
    "qt": {
        "name": "其他",
        "list_url": f"{BASE_URL}/gk/tzgg/qt/",
        "description": "通知公告 - 其他类",
    },
    "zcjd": {
        "name": "政策解读",
        "list_url": f"{BASE_URL}/gk/zcjd/",
        "description": "政策解读",
    },
}

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

logger = logging.getLogger("crawler_mem")


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S")


# 非政策文档的导航标题（需从结果中排除）
NAVIGATION_TITLES = {
    "首页", "机构", "新闻", "公开", "服务", "互动", "党建",
    "社会救援服务", "应急科普", "法律法规标准", "应急预案",
    "政务服务", "警示信息", "通知公告", "政策解读",
    "政府信息公开", "人事信息", "财务信息", "计划规划", "统计数据",
    "行政许可", "行政执法公示", "应急普法", "查询服务", "业务系统",
    "回应关切", "征求意见", "在线访谈", "公众留言",
    "党建要闻", "基层党建", "党建交流", "党风廉政",
    "规章制度", "学习园地", "群团统战", "巡视工作",
    "生活安全", "自然灾害", "安全生产", "应急科普场馆",
    "时政要闻", "应急要闻", "工作动态", "地方应急",
    "救援力量", "灾害事故信息", "新闻发布会", "媒体信息",
    "队伍风采", "工作信息", "事故及灾害查处", "电子证照",
    "应急管理部公报",
}


def sanitize_filename(name: str, max_len: int = 120) -> str:
    """将标题转为安全的文件名。"""
    # 移除 Windows/Unix 不允许的字符
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r"\s+", "_", name.strip())
    if len(name) > max_len:
        # 截断时保留扩展名或末尾
        name = name[:max_len]
    return name


def is_fire_related(title: str, keywords: Optional[list] = None) -> bool:
    """判断标题是否与消防相关。"""
    if keywords is None:
        keywords = FIRE_KEYWORDS
    title_lower = title
    for kw in keywords:
        if kw in title_lower:
            return True
    # 也检查"应急"这个宽泛词是否真的消防相关（排除非消防的场景）
    if "应急" in title_lower:
        non_fire_patterns = [
            "公共卫生", "地质灾害", "地震应急", "防汛",
            "抗旱", "气象", "防疫", "医疗", "食品安全",
        ]
        for pat in non_fire_patterns:
            if pat in title_lower:
                return False
        return True
    return False


def normalize_url(url: str, base_page_url: str = BASE_URL) -> str:
    """规范化 URL：基于当前页面 URL 补全相对路径。"""
    url = url.strip()
    if not url:
        return ""
    if url.startswith("http"):
        return url
    if url.startswith("//"):
        return "https:" + url
    return urljoin(base_page_url, url)


def extract_date(text: str) -> str:
    """从文本中提取日期，返回 YYYY-MM-DD 格式字符串。"""
    patterns = [
        r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日]?",
        r"(\d{4})(\d{2})(\d{2})",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            try:
                return f"{y:04d}-{mo:02d}-{d:02d}"
            except ValueError:
                continue
    return ""


def hash_url(url: str) -> str:
    """生成 URL 的短哈希，用作文件名前缀。"""
    return hashlib.md5(url.encode()).hexdigest()[:10]


# ---------------------------------------------------------------------------
# HTTP 会话
# ---------------------------------------------------------------------------


class MemSpider:
    """应急管理部爬虫核心类。"""

    def __init__(
        self,
        delay: float = DEFAULT_DELAY,
        max_retries: int = MAX_RETRIES,
        no_pdf: bool = False,
        verbose: bool = False,
    ):
        self.delay = delay
        self.max_retries = max_retries
        self.no_pdf = no_pdf
        self.verbose = verbose

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate",
            }
        )
        self.session.timeout = REQUEST_TIMEOUT
        self._last_request = 0.0

        # 统计
        self.stats = {
            "pages_visited": 0,
            "documents_found": 0,
            "fire_documents": 0,
            "pdfs_downloaded": 0,
            "pdfs_extracted": 0,
            "errors": 0,
        }

    def _rate_limit(self) -> None:
        """请求频率控制。"""
        elapsed = time.time() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request = time.time()

    def fetch(self, url: str, encoding: Optional[str] = None) -> Optional[str]:
        """
        GET 请求，带重试和频率控制。
        返回 HTML 文本，失败返回 None。
        """
        for attempt in range(self.max_retries):
            try:
                self._rate_limit()
                resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()

                if encoding:
                    resp.encoding = encoding
                elif resp.apparent_encoding:
                    resp.encoding = resp.apparent_encoding
                else:
                    # mem.gov.cn 默认 GBK/GB2312 系列
                    resp.encoding = resp.apparent_encoding or "utf-8"

                return resp.text
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else "?"
                logger.warning(
                    "HTTP %s on %s (attempt %d/%d)",
                    status,
                    url,
                    attempt + 1,
                    self.max_retries,
                )
                if status in (404, 410, 403):
                    return None  # 不可恢复
            except requests.RequestException as e:
                logger.warning(
                    "Request failed for %s: %s (attempt %d/%d)",
                    url, e, attempt + 1, self.max_retries,
                )
            if attempt < self.max_retries - 1:
                wait = RETRY_BACKOFF ** (attempt + 1)
                logger.debug("Retrying in %.1fs...", wait)
                time.sleep(wait)
        return None

    def download_pdf(self, pdf_url: str, save_path: Path) -> bool:
        """
        下载 PDF 文件到指定路径。
        返回是否成功。
        """
        if self.no_pdf:
            return False
        try:
            self._rate_limit()
            resp = self.session.get(pdf_url, timeout=60)
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "")
            if "application/pdf" in content_type or pdf_url.lower().endswith(".pdf"):
                save_path.parent.mkdir(parents=True, exist_ok=True)
                save_path.write_bytes(resp.content)
                self.stats["pdfs_downloaded"] += 1
                return True
            else:
                # 可能不是 PDF，检查 magic bytes
                if resp.content[:5] == b"%PDF-":
                    save_path.parent.mkdir(parents=True, exist_ok=True)
                    save_path.write_bytes(resp.content)
                    self.stats["pdfs_downloaded"] += 1
                    return True
                logger.debug("URL does not appear to be a PDF: %s", pdf_url)
                return False
        except Exception as e:
            logger.error("Failed to download PDF from %s: %s", pdf_url, e)
            self.stats["errors"] += 1
            return False


# ---------------------------------------------------------------------------
# PDF 文本提取
# ---------------------------------------------------------------------------


def extract_pdf_text(pdf_path: Path) -> str:
    """
    使用 pdfplumber 提取 PDF 文本。
    对于文本型 PDF，提取质量很高；
    对于扫描型 PDF，返回空字符串（需后续 OCR 处理）。

    也尝试 pymupdf 作为备用提取器。
    """
    text_parts = []

    # 方法 1: pdfplumber（主力）
    try:
        import pdfplumber
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page_num, page in enumerate(pdf.pages, 1):
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(f"\n--- 第 {page_num} 页 ---\n")
                    text_parts.append(page_text)
        if text_parts:
            return "".join(text_parts)
    except Exception as e:
        logger.debug("pdfplumber failed for %s: %s", pdf_path.name, e)

    # 方法 2: pymupdf（备用）
    try:
        import fitz
        doc = fitz.open(str(pdf_path))
        for page_num in range(len(doc)):
            page = doc[page_num]
            page_text = page.get_text()
            if page_text.strip():
                text_parts.append(f"\n--- 第 {page_num + 1} 页 ---\n")
                text_parts.append(page_text)
        doc.close()
        if text_parts:
            return "".join(text_parts)
    except Exception as e:
        logger.debug("pymupdf failed for %s: %s", pdf_path.name, e)

    # 如果是扫描型 PDF，返回空（标记为需 OCR）
    logger.info("PDF appears to be scanned (no extractable text): %s", pdf_path.name)
    return ""


def is_scanned_pdf(pdf_path: Path) -> bool:
    """检测 PDF 是否为扫描型（无可提取文本）。"""
    try:
        import fitz
        doc = fitz.open(str(pdf_path))
        for i in range(min(3, len(doc))):
            text = doc[i].get_text().strip()
            if len(text) > 50:
                doc.close()
                return False
        doc.close()
        return True
    except Exception:
        return False  # 无法判断


# ---------------------------------------------------------------------------
# 页面解析
# ---------------------------------------------------------------------------


def clean_mem_content(text: str) -> str:
    """清理 MEM 页面正文：移除站点导航文本、页头页尾垃圾信息。"""
    # MEM 网站统一的导航文本前缀（出现在 body fallback 中）
    nav_marker = "应急科普场馆"
    idx = text.find(nav_marker)
    if idx > 0 and idx < len(text) * 0.5:
        # 截断导航部分之后的正文
        after_nav = text[idx + len(nav_marker):]
        # 跳过后续可能的导航子项
        lines = after_nav.split("\n")
        content_start = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped and len(stripped) > 30:
                # 找到了非导航的实际内容
                content_start = i
                break
        if content_start > 0:
            text = "\n".join(lines[content_start:])

    # 移除页脚垃圾
    for footer_marker in [
        "主办单位：应急管理部",
        "网站标识码",
        "京ICP备",
        "网站地图",
    ]:
        idx = text.find(footer_marker)
        if idx > 0:
            text = text[:idx].strip()

    # 清理多余空白
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"^\s+", "", text)

    return text.strip()


def parse_detail_page(html: str, url: str) -> Optional[dict]:
    """
    解析 mem.gov.cn 详情页，提取结构化信息。

    返回 dict:
      - title: 标题
      - publish_date: 发布日期 (YYYY-MM-DD)
      - content: 正文内容 (纯文本)
      - source_url: 来源 URL
      - metadata: 元数据 dict（索引号、发文单位等）
    """
    soup = BeautifulSoup(html, "lxml")

    # --- 标题 ---
    title = ""
    # 尝试多个可能的选择器
    for selector in [
        "div.article h1",
        "div.article h2",
        "div.conTit h1",
        "div.main h1",
        "div.TRS_Editor h1",
        "div.xl_tit1",
        "div#Title",
        "h1",
        "title",
    ]:
        elem = soup.select_one(selector)
        if elem:
            title = elem.get_text(strip=True)
            if len(title) > 5:
                break

    # --- 发布日期 ---
    publish_date = ""
    # 从元数据表格提取
    meta_table = soup.find("table")
    if meta_table:
        for row in meta_table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            for cell in cells:
                text = cell.get_text(strip=True)
                if "发布日期" in text or "成文日期" in text:
                    date_match = extract_date(text)
                    if date_match:
                        publish_date = date_match
                        break
            if publish_date:
                break

    # 从页面文本提取日期作为备用
    if not publish_date:
        page_text = soup.get_text()
        date_patterns = [
            r"发布日期[：:]\s*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2})",
            r"(\d{4}[-/年]\d{1,2}[-/月]\d{1,2})",
        ]
        for pat in date_patterns:
            m = re.search(pat, page_text)
            if m:
                publish_date = extract_date(m.group(1))
                break

    # --- 元数据 ---
    metadata = {}
    if meta_table:
        for row in meta_table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) >= 2:
                for i in range(0, len(cells) - 1, 2):
                    key = cells[i].get_text(strip=True).rstrip("：:")
                    val = cells[i + 1].get_text(strip=True)
                    if key and val:
                        metadata[key] = val

    # --- 正文 ---
    # 尝试常见的内容区域选择器（按优先级）
    content = ""
    content_selectors = [
        "div.TRS_Editor",
        "div.article div.TRS_Editor",
        "div#Zoom",
        "div.main div.content",
        "div.con",
        "div.article",
        "div#content",
        "div.main",
    ]
    for sel in content_selectors:
        content_elem = soup.select_one(sel)
        if content_elem and len(content_elem.get_text(strip=True)) > 200:
            # 移除脚本和样式
            for tag in content_elem.find_all(["script", "style"]):
                tag.decompose()
            # 移除导航链接
            for tag in content_elem.find_all("a", class_="prev"):
                tag.decompose()
            content = content_elem.get_text("\n", strip=True)
            break

    if not content:
        # 兜底：从 body 提取并清理
        body = soup.find("body")
        if body:
            for tag in body.find_all(["script", "style", "nav", "footer"]):
                tag.decompose()
            content = body.get_text("\n", strip=True)

    # 统一清理
    content = clean_mem_content(content)

    # 如果 content 太短（基本上只有导航），尝试其他提取方式
    if len(content) < 500:
        # 尝试直接提取所有段落文本
        all_paragraphs = []
        for tag in soup.find_all(["p", "div", "span"]):
            text = tag.get_text(strip=True)
            cls = tag.get("class", [])
            cls_str = " ".join(cls) if cls else ""
            # 跳过导航类元素
            if any(nav_word in cls_str.lower() for nav_word in ["nav", "menu", "header", "footer", "bread"]):
                continue
            if len(text) > 50 and not text.startswith("首页"):
                all_paragraphs.append(text)
        if all_paragraphs:
            content = "\n\n".join(all_paragraphs)
            content = clean_mem_content(content)

    if not title and not content:
        return None

    return {
        "title": title,
        "publish_date": publish_date,
        "content": content.strip() if content else "",
        "source_url": url,
        "metadata": metadata,
    }


def parse_list_page(
    html: str, page_url: str
) -> list[dict]:
    """
    解析 mem.gov.cn 列表页，提取条目列表。

    page_url: 当前列表页的完整 URL（用于解析相对路径）

    每一条目返回: {title, url, date}
    """
    soup = BeautifulSoup(html, "lxml")
    entries = []

    # 部令/通知列表页：链接模式为 <a href="...">标题</a> 后面跟日期
    # mem.gov.cn 列表页可能有多种格式：
    # 1. <li><a href="...">标题</a><span>日期</span></li>
    # 2. 表格形式的政府信息公开列表

    # 尝试模式 1: <li> 列表
    for li in soup.find_all("li"):
        a_tag = li.find("a", href=True)
        if a_tag:
            href = a_tag.get("href", "").strip()
            if not href or "javascript" in href:
                continue
            title = a_tag.get_text(strip=True)
            if len(title) < 4:
                continue
            # 尝试从同级或父级提取日期
            date_str = ""
            date_span = li.find("span")
            if date_span:
                date_str = extract_date(date_span.get_text(strip=True))
            if not date_str:
                # 从整个 li 文本提取
                date_str = extract_date(li.get_text(strip=True))

            full_url = normalize_url(href, page_url)
            entries.append({
                "title": title,
                "url": full_url,
                "date": date_str,
            })

    # 尝试模式 2: 表格形式（政府信息公开）
    if not entries:
        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                a_tag = row.find("a", href=True)
                if a_tag:
                    title = a_tag.get_text(strip=True)
                    if len(title) < 4:
                        continue
                    href = a_tag.get("href", "").strip()
                    if not href or "javascript" in href:
                        continue
                    full_url = normalize_url(href, page_url)
                    cells = row.find_all("td")
                    date_str = ""
                    if cells:
                        date_str = extract_date(cells[-1].get_text(strip=True))
                    entries.append({
                        "title": title,
                        "url": full_url,
                        "date": date_str,
                    })

    # 尝试模式 3: <a> 标签模式（直接找所有链接）
    if not entries:
        content_div = (
            soup.find("div", class_="main")
            or soup.find("div", class_="con")
            or soup.find("body")
        )
        if content_div:
            for a_tag in content_div.find_all("a", href=True):
                href = a_tag.get("href", "").strip()
                if not href or "javascript" in href or href == "#":
                    continue
                title = a_tag.get_text(strip=True)
                if len(title) < 6:
                    continue
                # 过滤导航链接
                if any(
                    skip in title
                    for skip in ["首页", "上一页", "下一页", "尾页", "返回", "网站地图"]
                ):
                    continue
                full_url = normalize_url(href, page_url)
                # 尝试从后续文本中提取日期
                next_text = ""
                next_sib = a_tag.next_sibling
                while next_sib and len(next_text) < 30:
                    if hasattr(next_sib, "get_text"):
                        next_text += next_sib.get_text(strip=True)
                    else:
                        next_text += str(next_sib).strip()
                    next_sib = next_sib.next_sibling
                date_str = extract_date(next_text)
                if not date_str:
                    date_str = extract_date(title)

                entries.append({
                    "title": title,
                    "url": full_url,
                    "date": date_str,
                })

    # 去重（按 URL）
    seen_urls = set()
    unique_entries = []
    for entry in entries:
        url_key = entry["url"].rstrip("/")
        if url_key not in seen_urls:
            seen_urls.add(url_key)
            unique_entries.append(entry)

    return unique_entries


def find_pdf_links(html: str, page_url: str) -> list[str]:
    """从详情页 HTML 中提取 PDF 附件链接。"""
    soup = BeautifulSoup(html, "lxml")
    pdf_urls = []
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip().lower()
        if href.endswith(".pdf") or ".pdf?" in href:
            full = normalize_url(a_tag["href"], page_url)
            if full not in pdf_urls:
                pdf_urls.append(full)
    return pdf_urls


# ---------------------------------------------------------------------------
# 爬虫主流程
# ---------------------------------------------------------------------------


@dataclass
class CrawlerState:
    """爬虫断点续传状态。"""

    visited_urls: set = field(default_factory=set)
    processed_entries: list[dict] = field(default_factory=list)
    current_section: str = ""
    current_page: int = 0
    stats: dict = field(default_factory=dict)


def save_checkpoint(state: CrawlerState) -> None:
    """保存断点。"""
    data = {
        "visited_urls": list(state.visited_urls),
        "processed_entries": state.processed_entries,
        "current_section": state.current_section,
        "current_page": state.current_page,
        "stats": state.stats,
        "updated_at": datetime.now().isoformat(),
    }
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.debug("Checkpoint saved: %d entries, %d visited URLs",
                 len(state.processed_entries), len(state.visited_urls))


def load_checkpoint() -> Optional[CrawlerState]:
    """加载断点。"""
    if not CHECKPOINT_FILE.exists():
        return None
    data = json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
    state = CrawlerState(
        visited_urls=set(data.get("visited_urls", [])),
        processed_entries=data.get("processed_entries", []),
        current_section=data.get("current_section", ""),
        current_page=data.get("current_page", 0),
        stats=data.get("stats", {}),
    )
    logger.info("Checkpoint loaded: %d entries, resume from section=%r page=%d",
                len(state.processed_entries), state.current_section, state.current_page)
    return state


def save_results(entries: list[dict], output_path: Optional[Path] = None) -> None:
    """保存最终结果到 JSON 文件。"""
    if output_path is None:
        output_path = OUTPUT_FILE
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("Results saved: %d entries → %s", len(entries), output_path)


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="应急管理部 (mem.gov.cn) 政策法规爬虫 — 消防相关文件采集",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s                           # 默认：采集部令+通知+公告
  %(prog)s --sections bl             # 只采集部令
  %(prog)s --sections bl,tz,yjbgg    # 采集指定栏目
  %(prog)s --max-pages 5             # 每栏目最多抓 5 页（测试用）
  %(prog)s --resume                  # 断点续传
  %(prog)s --no-pdf                  # 跳过 PDF 下载
  %(prog)s --verbose                 # 详细日志输出

可用栏目:
  bl    - 部令（应急管理部令 1-19 号等）
  tz    - 通知
  yjbgg - 公告
  yj    - 意见
  h     - 函
  tb    - 通报
  qt    - 其他
  zcjd  - 政策解读
        """,
    )
    p.add_argument(
        "--sections",
        type=str,
        default="bl,tz,yjbgg",
        help="栏目代码，逗号分隔 (默认: bl,tz,yjbgg)",
    )
    p.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="每个栏目最多抓取页数 (0=不限制)",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="从上次中断处继续",
    )
    p.add_argument(
        "--no-pdf",
        action="store_true",
        help="跳过 PDF 下载和文本提取",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        help=f"请求间隔秒数 (默认: {DEFAULT_DELAY})",
    )
    p.add_argument(
        "--output",
        type=str,
        default=str(OUTPUT_FILE),
        help=f"输出 JSON 文件路径 (默认: {OUTPUT_FILE})",
    )
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="详细日志输出",
    )
    p.add_argument(
        "--list-sections",
        action="store_true",
        help="列出所有可用栏目并退出",
    )
    return p


def print_sections() -> None:
    """打印所有可用栏目信息。"""
    print("可用栏目:")
    print("-" * 60)
    for code, info in SECTIONS.items():
        print(f"  {code:6s}  {info['name']:8s}  {info['description']}")
    print("-" * 60)
    print("默认: bl,tz,yjbgg (部令 + 通知 + 公告)")


def run_crawler(args: argparse.Namespace) -> None:
    """主爬虫流程。"""
    setup_logging(args.verbose)

    if args.list_sections:
        print_sections()
        return

    # 确定要爬取的栏目
    requested = [s.strip() for s in args.sections.split(",") if s.strip()]
    sections_to_crawl = []
    for code in requested:
        if code in SECTIONS:
            sections_to_crawl.append((code, SECTIONS[code]))
        else:
            logger.warning("Unknown section code: %s (skipped)", code)

    if not sections_to_crawl:
        logger.error("No valid sections to crawl. Use --list-sections to see available options.")
        sys.exit(1)

    logger.info("Crawling sections: %s", ", ".join(s[0] for s in sections_to_crawl))
    logger.info("PDF download: %s", "disabled" if args.no_pdf else "enabled")
    logger.info("Output: %s", args.output)

    # 初始化
    spider = MemSpider(
        delay=args.delay,
        no_pdf=args.no_pdf,
        verbose=args.verbose,
    )

    state = load_checkpoint() if args.resume else CrawlerState()
    all_entries = state.processed_entries[:]  # 已处理的保留

    try:
        for section_code, section_info in sections_to_crawl:
            if args.resume and state.current_section:
                # 跳过已完成的栏目
                section_codes = [s[0] for s in sections_to_crawl]
                if section_codes.index(section_code) < section_codes.index(state.current_section):
                    logger.info("Skipping completed section: %s", section_code)
                    continue

            list_url = section_info["list_url"]
            logger.info("=" * 60)
            logger.info("Section: %s (%s)", section_code, section_info["name"])
            logger.info("List URL: %s", list_url)
            logger.info("=" * 60)

            # 爬取列表页（mem.gov.cn 的部令/通知等通常不分页，全部在一个页面）
            # 但某些栏目会分页，通过 index_{page}.shtml 模式
            page = 0
            if args.resume and section_code == state.current_section:
                page = state.current_page

            while True:
                if page == 0:
                    page_url = list_url
                else:
                    # mem.gov.cn 分页模式：将 index.shtml → index_{N}.shtml
                    # 或直接用 index_{N}.shtml 追加
                    if "index" in list_url:
                        page_url = re.sub(r"index(_\d+)?\.shtml", f"index_{page}.shtml", list_url)
                    else:
                        base_path = list_url.rstrip("/")
                        page_url = f"{base_path}/index_{page}.shtml"

                logger.info("Fetching list page %d: %s", page + 1, page_url)
                html = spider.fetch(page_url)
                if not html:
                    if page == 0:
                        logger.error("Failed to fetch first page of %s, skipping section", section_code)
                    else:
                        logger.info("No more pages (HTTP error on page %d)", page + 1)
                    break

                spider.stats["pages_visited"] += 1

                # 解析列表页
                entries = parse_list_page(html, page_url)
                logger.info("  Found %d entries on page %d", len(entries), page + 1)

                if not entries:
                    # 空页面 = 到达末页
                    logger.info("No entries found on page %d, assuming end of list", page + 1)
                    break

                # 处理每个条目
                for entry in entries:
                    detail_url = entry["url"]
                    if detail_url in state.visited_urls:
                        logger.debug("  Skip (already visited): %s", entry["title"][:50])
                        continue

                    state.visited_urls.add(detail_url)

                    # 排除导航/栏目页面
                    if entry["title"] in NAVIGATION_TITLES:
                        logger.debug("  Skip (navigation): %s", entry["title"])
                        continue

                    # 检查标题是否消防相关
                    if not is_fire_related(entry["title"]):
                        logger.debug("  Skip (not fire-related): %s", entry["title"][:60])
                        continue

                    spider.stats["fire_documents"] += 1
                    logger.info("  [FIRE] %s", entry["title"][:80])

                    # 抓取详情页
                    detail_html = spider.fetch(detail_url)
                    if not detail_html:
                        logger.warning("  Failed to fetch detail: %s", detail_url)
                        spider.stats["errors"] += 1
                        continue

                    doc = parse_detail_page(detail_html, detail_url)
                    if not doc:
                        logger.warning("  Failed to parse detail: %s", detail_url)
                        spider.stats["errors"] += 1
                        continue

                    spider.stats["documents_found"] += 1

                    # 使用详情页解析出的标题（更准确）
                    if doc["title"]:
                        doc_title = doc["title"]
                        if len(doc_title) > len(entry["title"]):
                            entry["title"] = doc_title

                    # 合并日期
                    if not entry.get("date") and doc.get("publish_date"):
                        entry["date"] = doc["publish_date"]

                    # PDF 附件
                    pdf_urls = find_pdf_links(detail_html, detail_url)
                    doc["pdf_paths"] = []
                    doc["pdf_texts"] = []
                    doc["pdf_scanned"] = []

                    for pdf_url in pdf_urls:
                        safe_name = sanitize_filename(doc["title"] or "untitled")
                        pdf_hash = hash_url(pdf_url)
                        pdf_filename = f"{pdf_hash}_{safe_name}.pdf"
                        pdf_path = PDF_DIR / pdf_filename

                        if spider.download_pdf(pdf_url, pdf_path):
                            doc["pdf_paths"].append(str(pdf_path))

                            # 提取 PDF 文本
                            if not args.no_pdf:
                                pdf_text = extract_pdf_text(pdf_path)
                                scanned = is_scanned_pdf(pdf_path)
                                doc["pdf_texts"].append(pdf_text)
                                doc["pdf_scanned"].append(scanned)
                                spider.stats["pdfs_extracted"] += 1
                                if scanned:
                                    logger.info("    PDF is scanned (needs OCR): %s", pdf_filename)
                                else:
                                    logger.info(
                                        "    PDF extracted: %s (%d chars)",
                                        pdf_filename,
                                        len(pdf_text),
                                    )

                    # 合并正文（优先 HTML 正文，PDF 作为补充）
                    final_content = doc.get("content", "")
                    if not final_content and doc.get("pdf_texts"):
                        final_content = "\n\n--- PDF 提取文本 ---\n\n".join(
                            t for t in doc["pdf_texts"] if t
                        )

                    record = {
                        "title": doc.get("title", entry.get("title", "")),
                        "publish_date": doc.get("publish_date", entry.get("date", "")),
                        "content": final_content,
                        "source_url": detail_url,
                        "pdf_paths": doc.get("pdf_paths", []),
                        "pdf_scanned": doc.get("pdf_scanned", []),
                        "metadata": doc.get("metadata", {}),
                        "section": section_code,
                        "section_name": section_info["name"],
                        "crawled_at": datetime.now().isoformat(),
                    }

                    all_entries.append(record)
                    state.processed_entries = all_entries

                    # 每 5 条保存一次断点
                    if spider.stats["fire_documents"] % 5 == 0:
                        state.current_section = section_code
                        state.current_page = page
                        state.stats = spider.stats.copy()
                        save_checkpoint(state)
                        save_results(all_entries, Path(args.output))

                # 检查是否达到最大页数限制
                page += 1
                if args.max_pages > 0 and page >= args.max_pages:
                    logger.info("Reached max_pages limit (%d) for section %s",
                                args.max_pages, section_code)
                    break

                # 如果首页就是全部（不分页），退出循环
                if page == 1 and len(entries) < 15:
                    logger.debug("Single-page section, done")
                    break

            # 栏目完成，更新 checkpoint
            state.current_section = section_code
            state.current_page = 0
            state.stats = spider.stats.copy()
            save_checkpoint(state)

    except KeyboardInterrupt:
        logger.warning("Interrupted by user, saving checkpoint...")
        state.stats = spider.stats.copy()
        save_checkpoint(state)
        save_results(all_entries, Path(args.output))
        logger.info("Progress saved. Use --resume to continue.")
        sys.exit(1)
    except Exception:
        logger.error("Unexpected error:\n%s", traceback.format_exc())
        state.stats = spider.stats.copy()
        save_checkpoint(state)
        save_results(all_entries, Path(args.output))
        raise

    # --- 最终输出 ---
    save_results(all_entries, Path(args.output))

    # 打印统计
    logger.info("=" * 60)
    logger.info("Crawl complete. Statistics:")
    logger.info("  Pages visited:     %d", spider.stats["pages_visited"])
    logger.info("  Documents found:   %d", spider.stats["documents_found"])
    logger.info("  Fire-related docs: %d", spider.stats["fire_documents"])
    logger.info("  PDFs downloaded:   %d", spider.stats["pdfs_downloaded"])
    logger.info("  PDFs extracted:    %d", spider.stats["pdfs_extracted"])
    logger.info("  Errors:            %d", spider.stats["errors"])
    logger.info("  Total output:      %d entries", len(all_entries))
    logger.info("  Output file:       %s", args.output)
    logger.info("=" * 60)

    # 清理断点文件（成功完成）
    if CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()
        logger.debug("Checkpoint file removed")


def main() -> None:
    parser = build_argparser()
    args = parser.parse_args()
    run_crawler(args)


if __name__ == "__main__":
    main()
