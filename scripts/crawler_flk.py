#!/usr/bin/env python3
"""
国家法律法规数据库 (flk.npc.gov.cn) 爬虫

功能：
  - 支持关键词搜索（默认 "消防"）
  - 支持分页获取全部结果
  - 逐条抓取正文内容（DOCX/HTML）
  - 文本清洗：去除格式标记、保留条款结构、提取元数据
  - 输出 JSON 文件到 data/laws/
  - 请求频率控制（1-2 秒间隔）、重试机制

依赖：
  pip install requests beautifulsoup4 python-docx lxml

用法：
  python crawler_flk.py                          # 默认搜索"消防"
  python crawler_flk.py --keyword "安全生产"      # 自定义关键词
  python crawler_flk.py --keyword "消防" --max-pages 3  # 限制页数
  python crawler_flk.py --type flfg --max-pages 5      # 按类型获取
  python crawler_flk.py --keyword "消防" --output my_results.json
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

BASE_URL = "https://flk.npc.gov.cn"
API_LIST_URL = "https://flk.npc.gov.cn/api/"
API_DETAIL_URL = "https://flk.npc.gov.cn/api/detail"
FILE_BASE_URL = "https://wb.flk.npc.gov.cn"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "laws"

# 法规类型代码映射
TYPE_MAP = {
    "flfg": "法律",
    "xzfg": "行政法规",
    "dfxfg": "地方性法规",
    "sfjs": "司法解释",
    "jcfg": "监察法规",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{BASE_URL}/index.html",
    "Connection": "keep-alive",
}

# 请求间隔 (秒)
MIN_DELAY = 1.0
MAX_DELAY = 2.0
# 最大重试次数
MAX_RETRIES = 3
# 重试退避基数 (秒)
RETRY_BACKOFF = 3

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

# 确保 logs 目录存在（在创建日志 handler 之前）
_LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
_LOGS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            _LOGS_DIR / "crawler_flk.log",
            mode="a",
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("crawler_flk")


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def ensure_dir(path: Path) -> None:
    """确保目录存在（同时创建 logs 目录）"""
    path.mkdir(parents=True, exist_ok=True)
    logs_dir = Path(__file__).resolve().parent.parent / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)


def safe_filename(text: str) -> str:
    """将文本转为安全的文件名"""
    # 移除 Windows/Linux 文件名不合法字符
    text = re.sub(r'[\\/:*?"<>|]', "_", text)
    # 限制长度
    if len(text) > 80:
        text = text[:80]
    return text.strip()


def jitter_delay() -> float:
    """返回 1-2 秒的随机延迟"""
    import random

    return MIN_DELAY + random.random() * (MAX_DELAY - MIN_DELAY)


# ---------------------------------------------------------------------------
# 文本清洗
# ---------------------------------------------------------------------------

def clean_docx_text(text: str) -> str:
    """清洗 DOCX 提取的文本"""
    # 移除多余空白行（保留单空行作为段落分隔）
    text = re.sub(r"\n{3,}", "\n\n", text)
    # 移除不可见字符
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return text.strip()


def clean_html_text(html: str) -> str:
    """从 HTML 中提取纯文本，保留段落结构"""
    soup = BeautifulSoup(html, "lxml")
    # 移除 script/style 标签
    for tag in soup(["script", "style", "meta", "link"]):
        tag.decompose()

    # 将块级元素替换为换行
    for tag in soup.find_all(["p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr"]):
        tag.insert_before("\n")

    text = soup.get_text()

    # 清理
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)

    return text.strip()


def extract_articles(content: str) -> List[Dict[str, Any]]:
    """从正文中提取条款结构"""
    articles = []

    # 匹配「第X条」模式（支持中文数字和阿拉伯数字）
    article_pattern = re.compile(
        r"(第[一二三四五六七八九十百零\d]+条)\s*(.*?)(?=第[一二三四五六七八九十百零\d]+条|\Z)",
        re.DOTALL,
    )

    for match in article_pattern.finditer(content):
        article_num = match.group(1).strip()
        article_text = match.group(2).strip()
        articles.append({"article": article_num, "content": article_text})

    # 匹配「第X章」模式
    chapter_pattern = re.compile(r"(第[一二三四五六七八九十百零\d]+章\s*.+?)(?=\n|第[一二三四五六七八九十百零\d]+章|\Z)")

    chapters = []
    for match in chapter_pattern.finditer(content):
        chapters.append(match.group(1).strip())

    return articles


def extract_metadata_from_text(text: str) -> Dict[str, str]:
    """从法律文本开头提取元数据（文号、发布日期、制定机关等）"""
    meta = {}
    first_lines = text.split("\n")[:10]

    # 提取标题（第一行非空）
    for line in first_lines:
        line = line.strip()
        if line and not line.startswith("（") and not line.startswith("("):
            meta["extracted_title"] = line
            break

    # 尝试提取公文编号（常见模式：XXXX第XX号）
    doc_no_pattern = re.compile(
        r"([一-鿿]+第[一-鿿\d]+号|[一-鿿]+〔\d+〕[一-鿿\d]+号)"
    )
    for line in first_lines:
        match = doc_no_pattern.search(line)
        if match:
            meta["document_number"] = match.group(1)
            break

    # 尝试提取日期
    date_pattern = re.compile(r"(\d{4}年\d{1,2}月\d{1,2}日)")
    dates = []
    for line in first_lines:
        for match in date_pattern.finditer(line):
            dates.append(match.group(1))
    if dates:
        if len(dates) >= 2:
            meta["publish_date_in_text"] = dates[0]
            meta["implement_date_in_text"] = dates[1]
        else:
            meta["publish_date_in_text"] = dates[0]

    return meta


# ---------------------------------------------------------------------------
# 核心爬虫类
# ---------------------------------------------------------------------------

class FlkCrawler:
    """国家法律法规数据库爬虫"""

    def __init__(
        self,
        keyword: str = "消防",
        law_type: str = "",
        max_pages: int = 0,
        page_size: int = 20,
        output_dir: Path = DEFAULT_OUTPUT_DIR,
        session: Optional[requests.Session] = None,
        delay_range: tuple = (MIN_DELAY, MAX_DELAY),
    ):
        self.keyword = keyword
        self.law_type = law_type
        self.max_pages = max_pages  # 0 = 不限制
        self.page_size = min(page_size, 100)  # API 最大支持 100
        self.output_dir = Path(output_dir)
        self.delay_range = delay_range

        # HTTP 会话
        self.session = session or requests.Session()
        self.session.headers.update(HEADERS)

        # 统计
        self.stats = {
            "total_laws_found": 0,
            "total_laws_downloaded": 0,
            "total_laws_failed": 0,
            "pages_crawled": 0,
            "errors": [],
        }

        # 结果存储
        self.results: List[Dict[str, Any]] = []

        ensure_dir(self.output_dir)

    # ------------------------------------------------------------------
    # HTTP 请求封装 (带重试)
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        retries: int = MAX_RETRIES,
        **kwargs,
    ) -> Optional[requests.Response]:
        """发送 HTTP 请求，带重试和退避"""
        for attempt in range(1, retries + 1):
            try:
                response = self.session.request(method, url, timeout=30, **kwargs)
                return response
            except requests.exceptions.Timeout:
                logger.warning(f"[超时] {method} {url} (第 {attempt}/{retries} 次)")
            except requests.exceptions.ConnectionError as e:
                logger.warning(f"[连接错误] {method} {url}: {e} (第 {attempt}/{retries} 次)")
            except requests.exceptions.RequestException as e:
                logger.warning(f"[请求异常] {method} {url}: {e} (第 {attempt}/{retries} 次)")

            if attempt < retries:
                wait = RETRY_BACKOFF * attempt
                logger.info(f"  等待 {wait}s 后重试...")
                time.sleep(wait)

        logger.error(f"[请求失败] {method} {url} (已重试 {retries} 次)")
        return None

    def _is_json_response(self, response: requests.Response) -> bool:
        """判断响应是否为 JSON"""
        content_type = response.headers.get("Content-Type", "")
        return "application/json" in content_type or response.text.strip().startswith("{")

    def _is_waf_blocked(self, response: requests.Response) -> bool:
        """判断响应是否被 WAF 拦截（返回 HTML 页面而非 JSON）"""
        return (
            response.status_code == 200
            and not self._is_json_response(response)
            and "<html" in response.text[:200].lower()
        )

    # ------------------------------------------------------------------
    # API: 搜索结果列表
    # ------------------------------------------------------------------

    def search_laws(
        self, page: int = 1, keyword: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        搜索法律法规列表

        返回格式:
        {
          "success": true,
          "result": {
            "totalSizes": 总数,
            "page": 当前页,
            "size": 每页条数,
            "data": [{id, title, office, publish, type, status}, ...]
          }
        }
        """
        kw = keyword if keyword is not None else self.keyword

        params: Dict[str, Any] = {
            "type": self.law_type,
            "sortTr": "f_bbrq_s;desc",
            "gbrqStart": "",
            "gbrqEnd": "",
            "sxrqStart": "",
            "sxrqEnd": "",
            "sort": "true",
            "page": str(page),
            "size": str(self.page_size),
            "_": str(int(time.time() * 1000)),
        }

        # 关键词搜索参数 (尝试多种可能的参数名)
        if kw:
            # 方式1: searchWord 参数 (最常见)
            params["searchWord"] = kw
            # searchType 指定标题模糊搜索
            params["searchType"] = "title;vague"
        else:
            params["searchType"] = "title;vague"

        logger.info(f"[搜索] 关键词='{kw}', 页码={page}, 每页{self.page_size}条")

        response = self._request("GET", API_LIST_URL, params=params)
        if response is None:
            return None

        # 检查是否被 WAF 拦截
        if self._is_waf_blocked(response):
            logger.error(
                "[WAF 拦截] API 返回 HTML 页面而非 JSON。"
                "服务器 IP 可能被阿里云 WAF 拦截，请尝试以下方案：\n"
                "  1. 使用浏览器访问 https://flk.npc.gov.cn 获取 acw_tc Cookie\n"
                "  2. 将 Cookie 通过环境变量 FLK_COOKIE 传入\n"
                "  3. 或使用 --use-selenium 参数启用浏览器自动化模式"
            )
            return None

        try:
            data = response.json()
        except json.JSONDecodeError:
            logger.error(f"[解析失败] 响应不是有效 JSON，前200字符: {response.text[:200]}")
            return None

        if not data.get("success"):
            logger.error(f"[API 错误] {data}")
            return None

        return data

    # ------------------------------------------------------------------
    # API: 获取法规详情 (下载链接)
    # ------------------------------------------------------------------

    def get_detail(self, law_id: str) -> Optional[Dict[str, Any]]:
        """
        获取法规详情，包含文件下载路径

        返回格式:
        {
          "success": true,
          "result": {
            "title": "...",
            "office": "...",
            "publish": "...",
            "body": [{"type": "WORD", "path": "/flfg/WORD/xxx.docx"}, ...]
          }
        }
        """
        logger.debug(f"[详情] id={law_id}")

        response = self._request("POST", API_DETAIL_URL, data={"id": law_id})
        if response is None:
            return None

        if self._is_waf_blocked(response):
            logger.warning(f"[WAF 拦截] 详情接口被拦截 id={law_id}")
            return None

        try:
            data = response.json()
        except json.JSONDecodeError:
            logger.error(f"[解析失败] 详情 id={law_id}")
            return None

        if not data.get("success"):
            logger.warning(f"[详情API] 返回失败 id={law_id}: {data}")
            return None

        return data

    # ------------------------------------------------------------------
    # 文件下载
    # ------------------------------------------------------------------

    def download_file(self, path: str) -> Optional[bytes]:
        """下载法规文件（DOCX/HTML）"""
        # 路径可能已经是完整 URL 或相对路径
        if path.startswith("http"):
            url = path
        else:
            url = FILE_BASE_URL + path

        logger.debug(f"[下载] {url}")

        response = self._request("GET", url)
        if response is None:
            return None

        return response.content

    # ------------------------------------------------------------------
    # 文本提取
    # ------------------------------------------------------------------

    def extract_text_from_docx(self, content: bytes) -> str:
        """从 DOCX 二进制数据中提取纯文本"""
        try:
            from docx import Document

            doc = Document(BytesIO(content))
            paragraphs = []
            for para in doc.paragraphs:
                text = para.text.strip()
                if text:
                    # 识别段落样式
                    if para.style and para.style.name:
                        style_name = para.style.name.lower()
                        if "heading" in style_name or "title" in style_name or "标题" in style_name:
                            paragraphs.append(f"\n{text}\n")
                        else:
                            paragraphs.append(text)
                    else:
                        paragraphs.append(text)

            text = "\n".join(paragraphs)
            return clean_docx_text(text)

        except ImportError:
            logger.warning("python-docx 未安装，无法解析 DOCX。请执行: pip install python-docx")
            return ""
        except Exception as e:
            logger.error(f"[DOCX 解析失败] {e}")
            return ""

    def extract_text_from_html(self, content: bytes) -> str:
        """从 HTML 中提取纯文本"""
        try:
            html = content.decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            try:
                html = content.decode("gbk", errors="replace")
            except UnicodeDecodeError:
                html = content.decode("utf-8", errors="ignore")
        return clean_html_text(html)

    def extract_text(self, content: bytes, doc_type: str = "WORD") -> str:
        """根据文件类型提取文本"""
        if doc_type.upper() == "WORD":
            text = self.extract_text_from_docx(content)
            if not text:
                # DOCX 解析失败，尝试当 HTML 处理
                logger.info("  DOCX 解析失败，尝试 HTML 格式")
                # 尝试获取 HTML 版本
                text = ""
            return text
        elif doc_type.upper() in ("HTML", "HTM"):
            return self.extract_text_from_html(content)
        elif doc_type.upper() == "PDF":
            logger.warning("  PDF 格式暂不支持文本提取")
            return "[PDF格式，未提取]"
        else:
            logger.warning(f"  未知文件类型: {doc_type}")
            return ""

    # ------------------------------------------------------------------
    # 主流程: 获取单条法律的完整信息
    # ------------------------------------------------------------------

    def fetch_law(self, item: Dict[str, Any], fetch_body: bool = True) -> Dict[str, Any]:
        """获取单条法律的完整信息和正文"""
        law_id = item.get("id", "")
        title = item.get("title", "未知标题")

        logger.info(f"[获取] {title} (id={law_id})")

        result = {
            "id": law_id,
            "title": title,
            "office": item.get("office", ""),
            "publish": item.get("publish", ""),
            "expiry": item.get("expiry", ""),
            "type": item.get("type", ""),
            "status": item.get("status", ""),
            "url": f"{BASE_URL}/detail2.html?{law_id}",
            "content": "",
            "articles": [],
            "metadata": {},
            "download_links": {},
        }

        if not fetch_body:
            return result

        # 获取详情
        detail = self.get_detail(law_id)
        if not detail:
            self.stats["total_laws_failed"] += 1
            self.stats["errors"].append(f"获取详情失败: {law_id} - {title}")
            return result

        detail_result = detail.get("result", {})

        # 更新元数据
        result["office"] = detail_result.get("office", result["office"])
        result["publish"] = detail_result.get("publish", result["publish"])
        result["title"] = detail_result.get("title", result["title"])

        # 获取下载链接
        body_files = detail_result.get("body", [])
        download_links = {}
        for f in body_files:
            file_type = f.get("type", "UNKNOWN")
            file_path = f.get("path", "")
            if file_path:
                download_links[file_type] = FILE_BASE_URL + file_path
        result["download_links"] = download_links

        # 优先下载 WORD 文件获取文本
        text_extracted = False
        for doc_type in ("WORD", "HTML", "HTM"):
            path = next(
                (f.get("path", "") for f in body_files if f.get("type", "").upper() == doc_type.upper()),
                "",
            )
            if path:
                file_content = self.download_file(path)
                if file_content:
                    text = self.extract_text(file_content, doc_type)
                    if text:
                        result["content"] = text
                        result["articles"] = extract_articles(text)
                        result["metadata"] = extract_metadata_from_text(text)
                        text_extracted = True
                        break
                    else:
                        logger.warning(f"  文本提取为空: {doc_type}")
                else:
                    logger.warning(f"  下载失败: {doc_type}")
            else:
                logger.debug(f"  无 {doc_type} 格式文件")

        if not text_extracted:
            logger.warning(f"  未能提取正文文本: {title}")
            self.stats["total_laws_failed"] += 1
            self.stats["errors"].append(f"文本提取失败: {law_id} - {title}")
        else:
            self.stats["total_laws_downloaded"] += 1
            logger.info(f"  [成功] {title} ({len(result['content'])} 字符, "
                        f"{len(result['articles'])} 条条款)")

        return result

    # ------------------------------------------------------------------
    # 主流程: 搜索并采集全部结果
    # ------------------------------------------------------------------

    def crawl(self, fetch_body: bool = True) -> List[Dict[str, Any]]:
        """执行完整搜索和采集流程"""
        logger.info("=" * 60)
        logger.info(f"开始采集: 关键词='{self.keyword}', 类型='{self.law_type or '全部'}'")
        logger.info(f"输出目录: {self.output_dir}")
        logger.info("=" * 60)

        # 第 1 页：获取总数和首页数据
        first_page = self.search_laws(page=1)
        if first_page is None:
            logger.error("搜索 API 访问失败，采集终止。")
            return []

        result = first_page.get("result", {})
        total = result.get("totalSizes", 0)
        page = result.get("page", 1)
        size = result.get("size", self.page_size)
        total_pages = (total + size - 1) // size if total > 0 else 1

        logger.info(f"共找到 {total} 条结果，{total_pages} 页（每页 {size} 条）")
        self.stats["total_laws_found"] = total

        if self.max_pages > 0:
            total_pages = min(total_pages, self.max_pages)
            logger.info(f"已限制最大页数: {self.max_pages}")

        # 处理首页数据
        items_page_1 = result.get("data", [])
        self._process_page_items(items_page_1, fetch_body)
        self.stats["pages_crawled"] = 1

        # 处理剩余页
        for p in range(2, total_pages + 1):
            logger.info(f"\n--- 第 {p}/{total_pages} 页 ---")

            # 限速延迟
            delay = jitter_delay()
            time.sleep(delay)

            page_data = self.search_laws(page=p)
            if page_data is None:
                logger.warning(f"第 {p} 页获取失败，跳过")
                continue

            items = page_data.get("result", {}).get("data", [])
            self._process_page_items(items, fetch_body)
            self.stats["pages_crawled"] += 1

        # 保存结果
        self._save_results()

        # 输出统计信息
        self._print_stats()

        return self.results

    def _process_page_items(
        self, items: List[Dict[str, Any]], fetch_body: bool
    ) -> None:
        """处理单页的法律条款列表"""
        for item in items:
            # 立即输出基本信息（正文抓取前）
            law_data = self.fetch_law(item, fetch_body=fetch_body)
            self.results.append(law_data)

            # 限速
            if fetch_body:
                time.sleep(jitter_delay())

    # ------------------------------------------------------------------
    # 结果保存
    # ------------------------------------------------------------------

    def _save_results(self) -> None:
        """保存结果到 JSON 文件"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        keyword_slug = safe_filename(self.keyword) if self.keyword else "all"
        filename = f"flk_{keyword_slug}_{timestamp}.json"
        filepath = self.output_dir / filename

        output = {
            "metadata": {
                "source": "国家法律法规数据库 (flk.npc.gov.cn)",
                "keyword": self.keyword,
                "law_type": self.law_type,
                "crawl_time": datetime.now().isoformat(),
                "stats": self.stats,
            },
            "results": self.results,
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

        logger.info(f"\n[保存] 结果已保存至: {filepath}")

        # 同时保存一份简洁的 CSV 汇总
        csv_path = filepath.with_suffix(".csv")
        self._save_csv_summary(csv_path)

    def _save_csv_summary(self, filepath: Path) -> None:
        """保存 CSV 汇总文件"""
        import csv

        with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["序号", "标题", "制定机关", "类型", "发布日期", "状态", "字符数", "条款数", "URL"])
            for i, law in enumerate(self.results, 1):
                writer.writerow([
                    i,
                    law.get("title", ""),
                    law.get("office", ""),
                    law.get("type", ""),
                    law.get("publish", ""),
                    law.get("status", ""),
                    len(law.get("content", "")),
                    len(law.get("articles", [])),
                    law.get("url", ""),
                ])

        logger.info(f"[保存] CSV 汇总已保存至: {filepath}")

    def _print_stats(self) -> None:
        """打印采集统计"""
        print("\n" + "=" * 60)
        print("采集完成！统计信息：")
        print(f"  搜索结果总数: {self.stats['total_laws_found']}")
        print(f"  成功获取正文: {self.stats['total_laws_downloaded']}")
        print(f"  正文获取失败: {self.stats['total_laws_failed']}")
        print(f"  已采集页数:   {self.stats['pages_crawled']}")
        print(f"  输出目录:     {self.output_dir}")
        if self.stats["errors"]:
            print(f"  错误详情:     {len(self.stats['errors'])} 条")
            for err in self.stats["errors"][:5]:
                print(f"    - {err}")
            if len(self.stats["errors"]) > 5:
                print(f"    ... 及其他 {len(self.stats['errors'])-5} 条")
        print("=" * 60)


# ---------------------------------------------------------------------------
# Selenium 浏览器自动化爬虫 (绕过 WAF)
# ---------------------------------------------------------------------------

class FlkCrawlerSelenium(FlkCrawler):
    """
    基于 Selenium 的浏览器自动化爬虫，用于绕过阿里云 WAF

    当直接 HTTP 请求被 WAF 拦截时，使用此模式通过真实浏览器访问网站。
    需要安装 Selenium 和浏览器驱动：

      pip install selenium webdriver-manager
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._driver = None

    def _get_driver(self):
        """获取或创建 Selenium WebDriver"""
        if self._driver is not None:
            return self._driver

        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC

            self.webdriver = webdriver
            self.By = By
            self.WebDriverWait = WebDriverWait
            self.EC = EC

            options = Options()
            options.add_argument("--headless=new")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--window-size=1920,1080")
            options.add_argument(f"user-agent={HEADERS['User-Agent']}")
            options.add_argument("--lang=zh-CN")
            options.add_experimental_option("prefs", {
                "profile.default_content_setting_values.images": 2,  # 不加载图片
            })

            # 尝试使用 webdriver-manager 自动获取驱动
            try:
                from webdriver_manager.chrome import ChromeDriverManager
                service = Service(ChromeDriverManager().install())
                driver = webdriver.Chrome(service=service, options=options)
                logger.info("[Selenium] ChromeDriver 已通过 webdriver-manager 自动配置")
            except (ImportError, Exception):
                driver = webdriver.Chrome(options=options)
                logger.info("[Selenium] 使用系统默认 ChromeDriver")

            driver.set_page_load_timeout(30)
            self._driver = driver
            return driver

        except ImportError as e:
            raise ImportError(
                "Selenium 模式需要安装依赖: pip install selenium webdriver-manager"
            ) from e
        except Exception as e:
            logger.error(f"[Selenium] WebDriver 创建失败: {e}")
            raise

    def search_laws(self, page: int = 1, keyword: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        通过 Selenium 浏览器访问 API 获取搜索结果

        策略：用浏览器先访问主页完成 WAF 挑战，获取 Cookie，
        然后使用 requests 调用 API。
        """
        kw = keyword if keyword is not None else self.keyword

        if page == 1:
            # 首次访问：用浏览器完成 WAF 验证
            logger.info("[Selenium] 启动浏览器，完成 WAF 验证...")
            driver = self._get_driver()

            try:
                driver.get(BASE_URL + "/")
                time.sleep(5)  # 等待 WAF JS 挑战完成

                # 将浏览器 Cookie 复制到 requests session
                selenium_cookies = driver.get_cookies()
                for cookie in selenium_cookies:
                    self.session.cookies.set(
                        cookie["name"], cookie["value"],
                        domain=cookie.get("domain", ""),
                    )
                logger.info(f"[Selenium] 已获取 {len(selenium_cookies)} 个 Cookie")
            except Exception as e:
                logger.error(f"[Selenium] 主页加载失败: {e}")

        # 使用 requests + 浏览器 Cookie 调用 API
        return super().search_laws(page=page, keyword=kw)

    def get_detail(self, law_id: str) -> Optional[Dict[str, Any]]:
        """通过 requests + Cookie 获取详情（Cookie 已从 Selenium 获取）"""
        return super().get_detail(law_id)

    def download_file(self, path: str) -> Optional[bytes]:
        """通过 requests + Cookie 下载文件"""
        return super().download_file(path)

    def close(self):
        """关闭浏览器"""
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None

    def __del__(self):
        self.close()


# ---------------------------------------------------------------------------
# WAF Cookie 辅助
# ---------------------------------------------------------------------------

def try_get_waf_cookie() -> Optional[str]:
    """
    尝试通过访问主页获取 WAF Cookie

    阿里云 WAF 会通过 JS 挑战设置 acw_tc Cookie，
    但需要浏览器执行 JS 才能获得。纯 HTTP 请求无法获取。
    """
    try:
        response = requests.get(
            f"{BASE_URL}/",
            headers={
                "User-Agent": HEADERS["User-Agent"],
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=15,
        )
        cookies = response.cookies
        if cookies:
            logger.info(f"[Cookie] 获取到 {len(cookies)} 个 Cookie")
            return "; ".join(f"{k}={v}" for k, v in cookies.items())
    except Exception as e:
        logger.debug(f"[Cookie] 获取失败: {e}")

    return None


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="国家法律法规数据库爬虫 (flk.npc.gov.cn)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python crawler_flk.py                                      # 默认搜索"消防"
  python crawler_flk.py --keyword "安全生产法"                # 自定义关键词
  python crawler_flk.py --keyword "消防" --max-pages 3        # 限制页数
  python crawler_flk.py --type flfg --max-pages 5             # 按类型获取
  python crawler_flk.py --keyword "消防" --page-size 50       # 每页50条
  python crawler_flk.py --keyword "消防" --no-body             # 仅获取列表
  python crawler_flk.py --cookie "acw_tc=xxx" --keyword "消防" # 带 Cookie
        """,
    )
    parser.add_argument(
        "--keyword", "-k",
        type=str,
        default="消防",
        help="搜索关键词 (默认: 消防)",
    )
    parser.add_argument(
        "--type", "-t",
        type=str,
        default="",
        choices=["", "flfg", "xzfg", "dfxfg", "sfjs", "jcfg"],
        help="法规类型 (默认: 全部)",
    )
    parser.add_argument(
        "--max-pages", "-p",
        type=int,
        default=0,
        help="最大采集页数 (0=不限制, 默认: 0)",
    )
    parser.add_argument(
        "--page-size", "-s",
        type=int,
        default=20,
        help="每页条数 (最大100, 默认: 20)",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default="",
        help="输出文件名 (默认: 自动生成)",
    )
    parser.add_argument(
        "--output-dir", "-d",
        type=str,
        default=str(DEFAULT_OUTPUT_DIR),
        help="输出目录路径",
    )
    parser.add_argument(
        "--no-body",
        action="store_true",
        help="仅获取列表元数据，不下载正文",
    )
    parser.add_argument(
        "--cookie",
        type=str,
        default=os.environ.get("FLK_COOKIE", ""),
        help="设置 Cookie (也可通过环境变量 FLK_COOKIE 设置)",
    )
    parser.add_argument(
        "--delay-min",
        type=float,
        default=MIN_DELAY,
        help=f"最小请求间隔秒数 (默认: {MIN_DELAY})",
    )
    parser.add_argument(
        "--delay-max",
        type=float,
        default=MAX_DELAY,
        help=f"最大请求间隔秒数 (默认: {MAX_DELAY})",
    )
    parser.add_argument(
        "--use-selenium",
        action="store_true",
        help="使用 Selenium 浏览器自动化绕过 WAF（需安装 selenium 和 ChromeDriver）",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="详细日志输出",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # 构建输出目录
    output_dir = Path(args.output_dir)
    ensure_dir(output_dir)

    logger.info(f"输出目录: {output_dir}")
    logger.info(f"关键词: {args.keyword}")
    logger.info(f"类型过滤: {args.type or '全部'}")
    logger.info(f"最大页数: {args.max_pages or '不限制'}")
    logger.info(f"每页条数: {args.page_size}")
    logger.info(f"下载正文: {'否' if args.no_body else '是'}")
    logger.info(f"浏览器模式: {'是 (Selenium)' if args.use_selenium else '否 (requests)'}")

    # 创建 Session
    session = requests.Session()

    # 设置 Cookie
    if args.cookie:
        logger.info("使用自定义 Cookie")
        for cookie_pair in args.cookie.split(";"):
            cookie_pair = cookie_pair.strip()
            if "=" in cookie_pair:
                key, value = cookie_pair.split("=", 1)
                session.cookies.set(key.strip(), value.strip())

    # 判断使用哪种爬虫模式
    use_selenium = args.use_selenium
    if not args.cookie and not session.cookies:
        logger.info("尝试获取 WAF Cookie...")
        waf_cookie = try_get_waf_cookie()
        if waf_cookie:
            for cookie_pair in waf_cookie.split(";"):
                cookie_pair = cookie_pair.strip()
                if "=" in cookie_pair:
                    key, value = cookie_pair.split("=", 1)
                    session.cookies.set(key.strip(), value.strip())
        else:
            logger.warning(
                "⚠ 未获取到 WAF Cookie。"
                "如果 API 返回 HTML 而非 JSON，说明被 WAF 拦截。\n"
                "请通过以下方式解决：\n"
                "  1. 浏览器访问 https://flk.npc.gov.cn\n"
                "  2. F12 开发者工具 > Application > Cookies\n"
                "  3. 复制 acw_tc Cookie 值\n"
                "  4. 运行: python crawler_flk.py --cookie 'acw_tc=你的Cookie值'\n"
                "  5. 或设置环境变量: export FLK_COOKIE='acw_tc=你的Cookie值'\n"
                "  6. 或使用 Selenium 模式: python crawler_flk.py --use-selenium"
            )

    # 创建爬虫实例
    crawler_cls = FlkCrawlerSelenium if use_selenium else FlkCrawler
    crawler_kwargs = dict(
        keyword=args.keyword,
        law_type=args.type,
        max_pages=args.max_pages,
        page_size=args.page_size,
        output_dir=output_dir,
        session=session,
        delay_range=(args.delay_min, args.delay_max),
    )

    crawler = crawler_cls(**crawler_kwargs)

    try:
        results = crawler.crawl(fetch_body=not args.no_body)
        if not results:
            logger.warning("未获取到任何结果，请检查网络连接和 API 访问")
            sys.exit(1)
    except KeyboardInterrupt:
        logger.info("\n用户中断，保存当前结果...")
        crawler._save_results()
        crawler._print_stats()
        sys.exit(0)
    except Exception as e:
        logger.exception(f"采集异常: {e}")
        # 尝试保存部分结果
        if crawler.results:
            crawler._save_results()
        sys.exit(1)
    finally:
        if hasattr(crawler, "close"):
            crawler.close()


if __name__ == "__main__":
    main()
