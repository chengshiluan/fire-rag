#!/usr/bin/env python3
"""
消防 RAG 知识库 — 爬虫基类 (BaseCrawler)

提供所有数据源采集器共享的基础设施：
- 统一的请求管理（Session、重试、频率控制）
- 统一的日志系统（结构化 JSON 行日志）
- 统一的断点续传机制
- 统一的文本清洗工具
- 统一的数据输出格式

这是所有 Adapter 的底层依赖，不得被绕过。
"""

import hashlib
import json
import logging
import os
import re
import time
import threading
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# 常量定义
# ---------------------------------------------------------------------------

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 30  # 秒
DEFAULT_RETRY_COUNT = 3
DEFAULT_RETRY_BACKOFF = 5  # 秒，指数退避基数
DEFAULT_REQUEST_INTERVAL = 2.0  # 秒，请求间隔
MAX_BACKOFF = 300  # 最大退避等待秒数

# HTTP 状态码分类
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
CLIENT_ERROR_STATUSES = {400, 401, 403, 404}


# ---------------------------------------------------------------------------
# 结构化日志
# ---------------------------------------------------------------------------

class CrawlLogger:
    """结构化 JSON 行日志，便于后续分析和问题排查。"""

    def __init__(self, name: str, log_dir: str = "logs"):
        self.name = name
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self._logger = logging.getLogger(name)
        self._logger.setLevel(logging.DEBUG)

        # 控制台 handler
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(logging.Formatter(
            "[%(asctime)s] %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        self._logger.addHandler(ch)

        # 文件 handler（JSON 行格式）
        log_file = self.log_dir / f"{name}_{datetime.now().strftime('%Y%m%d')}.jsonl"
        fh = logging.FileHandler(str(log_file), encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(JsonLinesFormatter())
        self._logger.addHandler(fh)

    def _emit(self, level: str, event: str, **kwargs):
        extra: Dict[str, Any] = {"event": event, "crawler": self.name}
        extra.update(kwargs)
        record = logging.LogRecord(
            name=self.name,
            level=getattr(logging, level.upper()),
            pathname="",
            lineno=0,
            msg=event,
            args=(),
            exc_info=None,
        )
        record.__dict__.update(extra)
        self._logger.handle(record)

    def log_crawl(self, url: str, status: int, duration_ms: float, **kwargs):
        self._emit("info", "crawl", url=url, status=status,
                   duration_ms=round(duration_ms, 2), **kwargs)

    def log_error(self, url: str, error: str, **kwargs):
        self._emit("error", "error", url=url, error=error, **kwargs)

    def log_stat(self, source: str, stat: str, value: Any, **kwargs):
        self._emit("info", "stat", source=source, stat=stat, value=value, **kwargs)

    def log_checkpoint(self, checkpoint_id: str, progress: Dict[str, Any]):
        self._emit("info", "checkpoint", checkpoint_id=checkpoint_id, progress=progress)

    def log_download(self, url: str, file_path: str, size_bytes: int, duration_ms: float):
        self._emit("info", "download", url=url, file_path=file_path,
                   size_bytes=size_bytes, duration_ms=round(duration_ms, 2))


class JsonLinesFormatter(logging.Formatter):
    """将日志格式化为 JSON 行（每行一个 JSON 对象）。"""
    def format(self, record: logging.LogRecord) -> str:
        obj: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
        }
        # 将 record.__dict__ 中自定义字段合并
        for key in dir(record):
            if key in ("timestamp", "level", "message", "msg", "args",
                       "exc_info", "exc_text", "stack_info", "created",
                       "msecs", "relativeCreated", "thread", "threadName",
                       "process", "processName", "module", "funcName",
                       "lineno", "pathname", "filename", "name", "levelno",
                       "levelname", "getMessage"):
                continue
            val = getattr(record, key, None)
            if val is not None and not key.startswith("_"):
                obj[key] = val
        obj["message"] = record.getMessage()
        return json.dumps(obj, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 令牌桶频率控制
# ---------------------------------------------------------------------------

class RateLimiter:
    """
    基于令牌桶算法的频率控制器。

    使用方式：
        limiter = RateLimiter(interval=2.0)  # 每 2 秒发一个请求
        limiter.wait()  # 阻塞直到可以发出请求
    """

    def __init__(self, interval: float = DEFAULT_REQUEST_INTERVAL):
        self.interval = interval
        self._last_request_time: float = 0.0
        self._lock = threading.Lock()

    def wait(self):
        """阻塞当前线程直至可以发出下一个请求。"""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request_time
            if elapsed < self.interval:
                time.sleep(self.interval - elapsed)
            self._last_request_time = time.monotonic()

    @property
    def last_request_time(self) -> float:
        return self._last_request_time


# ---------------------------------------------------------------------------
# 重试处理
# ---------------------------------------------------------------------------

class RetryHandler:
    """
    指数退避重试处理器。

    用法：
        handler = RetryHandler(max_retries=3, base_backoff=5)
        result = handler.execute_with_retry(lambda: requests.get(url))
    """

    def __init__(
        self,
        max_retries: int = DEFAULT_RETRY_COUNT,
        base_backoff: float = DEFAULT_RETRY_BACKOFF,
        max_backoff: float = MAX_BACKOFF,
    ):
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff

    def execute_with_retry(
        self,
        func,
        *args,
        retryable_statuses: Optional[set] = None,
        on_retry: Optional[callable] = None,
        **kwargs,
    ) -> requests.Response:
        """
        执行带自动重试的 HTTP 请求。

        Args:
            func: 可调用对象（通常是 requests.get/requests.post）
            retryable_statuses: 自定义可重试状态码集合，默认 {429, 500, 502, 503, 504}
            on_retry: 重试时的回调 (attempt, error) -> None

        Returns:
            requests.Response 对象

        Raises:
            requests.RequestException: 所有重试均失败后抛出最后的异常
        """
        if retryable_statuses is None:
            retryable_statuses = RETRYABLE_STATUSES

        last_exception: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            try:
                response = func(*args, **kwargs)

                # 如果状态码在可重试范围内，抛出以便重试
                if response.status_code in retryable_statuses:
                    raise requests.HTTPError(
                        f"Retryable status {response.status_code}",
                        response=response,
                    )

                return response

            except requests.HTTPError as e:
                last_exception = e
                if e.response is not None and e.response.status_code in CLIENT_ERROR_STATUSES:
                    # 客户端错误（4xx，非 429）不重试
                    raise
                # 429 或其他可重试状态码 -> 进入退避重试
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exception = e

            if attempt < self.max_retries:
                backoff = min(self.base_backoff * (2 ** attempt), self.max_backoff)
                if on_retry:
                    on_retry(attempt + 1, last_exception)
                time.sleep(backoff)

        raise last_exception  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 请求管理
# ---------------------------------------------------------------------------

class RequestManager:
    """
    统一的 HTTP 请求管理器。

    特性：
    - requests.Session 封装（连接复用、Cookie 持久化）
    - 自动添加 User-Agent 和通用头
    - 支持代理配置
    - 集成 RateLimiter
    """

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: int = DEFAULT_TIMEOUT,
        rate_limiter: Optional[RateLimiter] = None,
        proxy: Optional[str] = None,
        verify_ssl: bool = True,
        extra_headers: Optional[Dict[str, str]] = None,
    ):
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.proxy = proxy
        self.rate_limiter = rate_limiter or RateLimiter()

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        })

        if extra_headers:
            self.session.headers.update(extra_headers)

        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

    def get(self, url: str, **kwargs) -> requests.Response:
        """发送 GET 请求（含频率控制）。"""
        self.rate_limiter.wait()
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("verify", self.verify_ssl)
        return self.session.get(url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        """发送 POST 请求（含频率控制）。"""
        self.rate_limiter.wait()
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("verify", self.verify_ssl)
        return self.session.post(url, **kwargs)

    def download_file(
        self,
        url: str,
        dest_path: str,
        chunk_size: int = 8192,
        **kwargs,
    ) -> Tuple[str, int]:
        """
        流式下载文件到本地。

        Returns:
            (文件绝对路径, 文件大小字节数)
        """
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)

        self.rate_limiter.wait()
        kwargs.setdefault("timeout", self.timeout * 3)  # 下载超时放宽
        kwargs.setdefault("verify", self.verify_ssl)
        kwargs.setdefault("stream", True)

        response = self.session.get(url, **kwargs)
        response.raise_for_status()

        total_bytes = 0
        with open(dest, "wb") as f:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    total_bytes += len(chunk)

        return str(dest.resolve()), total_bytes


# ---------------------------------------------------------------------------
# 断点续传
# ---------------------------------------------------------------------------

class CheckpointManager:
    """
    基于 JSON 文件的断点续传管理器。

    存储结构：
        {
            "crawler": "flk_crawler",
            "source": "flk.npc.gov.cn",
            "created_at": "2026-06-12T10:00:00Z",
            "updated_at": "2026-06-12T11:30:00Z",
            "total_items": 150,
            "completed_items": 87,
            "completed_ids": ["id1", "id2", ...],
            "last_page": 8,
            "last_cursor": "...",
            "stats": {
                "success": 85,
                "failed": 2,
                "skipped": 0
            }
        }

    用法：
        cpm = CheckpointManager("checkpoints/flk_crawler.json")
        cpm.save({"last_page": 5, "completed_ids": ["a", "b"]})
        data = cpm.load()
    """

    def __init__(self, checkpoint_path: str, crawler_name: str = "", source: str = ""):
        self.path = Path(checkpoint_path)
        self.crawler_name = crawler_name
        self.source = source

    def load(self) -> Dict[str, Any]:
        """加载检查点数据。不存在则返回空字典。"""
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return {}
        return {}

    def save(self, progress: Dict[str, Any]):
        """保存检查点数据（增量合并）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.load()

        # 合并
        existing.update(progress)
        existing.setdefault("crawler", self.crawler_name)
        existing.setdefault("source", self.source)
        existing.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        existing["updated_at"] = datetime.now(timezone.utc).isoformat()

        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)

    def mark_complete(self, item_id: str):
        """标记单个条目已完成。"""
        data = self.load()
        completed: List[str] = data.get("completed_ids", [])
        if item_id not in completed:
            completed.append(item_id)
        stats = data.get("stats", {})
        stats["success"] = stats.get("success", 0) + 1
        self.save({
            "completed_ids": completed,
            "completed_items": len(completed),
            "stats": stats,
        })

    def mark_failed(self, item_id: str):
        """标记单个条目失败。"""
        data = self.load()
        stats = data.get("stats", {})
        stats["failed"] = stats.get("failed", 0) + 1
        # 记录失败的 ID 便于后续重试
        failed_ids: List[Dict[str, str]] = data.get("failed_items", [])
        failed_ids.append({"id": item_id, "time": datetime.now(timezone.utc).isoformat()})
        self.save({"stats": stats, "failed_items": failed_ids})

    def get_pending_ids(self, all_ids: List[str]) -> List[str]:
        """返回尚未完成的 ID 列表。"""
        data = self.load()
        completed = set(data.get("completed_ids", []))
        return [i for i in all_ids if i not in completed]


# ---------------------------------------------------------------------------
# 文本清洗工具
# ---------------------------------------------------------------------------

class TextCleaner:
    """数据清洗工具函数集。"""

    # 中文数字映射
    _CN_NUM_MAP = {
        "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
        "十": 10, "百": 100, "千": 1000,
    }

    @staticmethod
    def clean_html(html_content: str, preserve_links: bool = False) -> str:
        """
        从 HTML 中提取纯文本，去除脚本、样式、导航等非正文元素。

        Args:
            html_content: 原始 HTML 字符串
            preserve_links: 是否保留链接 URL（在括号中追加）

        Returns:
            清洗后的纯文本
        """
        soup = BeautifulSoup(html_content, "lxml")

        # 移除不需要的元素
        for tag_name in ["script", "style", "nav", "footer", "header", "noscript"]:
            for tag in soup.find_all(tag_name):
                tag.decompose()

        # 移除常见非正文 class/id
        skip_patterns = [
            "nav", "menu", "sidebar", "footer", "header", "banner",
            "breadcrumb", "pagination", "comment", "advertisement",
            "copyright", "toolbar", "search",
        ]
        for pattern in skip_patterns:
            for tag in soup.find_all(class_=re.compile(pattern, re.I)):
                tag.decompose()
            for tag in soup.find_all(id=re.compile(pattern, re.I)):
                tag.decompose()

        if preserve_links:
            for a_tag in soup.find_all("a", href=True):
                href = a_tag["href"]
                a_tag.append(f" [{href}]")

        text = soup.get_text(separator="\n")
        return TextCleaner.normalize_text(text)

    @staticmethod
    def normalize_text(text: str) -> str:
        """
        文本规范化：
        - 统一换行为单个 \\n
        - 合并连续空白行
        - 去除首尾空白
        - 统一全角半角字符
        """
        # 将连续3个以上换行压缩为2个换行（保留段落间距）
        text = re.sub(r"\n{3,}", "\n\n", text)
        # 去除每行首尾空格
        lines = [line.strip() for line in text.split("\n")]
        # 去除前导空行
        while lines and not lines[0]:
            lines.pop(0)
        # 去除尾部空行
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    @staticmethod
    def extract_date(text: str) -> Optional[str]:
        """
        从文本中提取日期（YYYY-MM-DD 或 YYYY年MM月DD日）。

        Returns:
            'YYYY-MM-DD' 格式字符串，未找到返回 None
        """
        patterns = [
            r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?",
            r"(\d{4})(\d{2})(\d{2})",
        ]
        for pattern in patterns:
            m = re.search(pattern, text)
            if m:
                year, month, day = m.group(1), m.group(2), m.group(3)
                return f"{year}-{int(month):02d}-{int(day):02d}"
        return None

    @staticmethod
    def extract_chapter_info(text: str) -> Optional[Dict[str, str]]:
        """
        从一行文本中提取章节信息。

        支持格式：
        - "第一章 总则"
        - "第1章 总则"
        - "第二章  火灾预防"
        - "1 范围"（GB 标准）

        Returns:
            {"number": "一", "title": "总则"} 或 None
        """
        patterns = [
            r"第([一二三四五六七八九十百零\d]+)章\s*(.*)",
            r"^(\d+)\s+(.*)",
        ]
        for pattern in patterns:
            m = re.match(pattern, text.strip())
            if m:
                return {"number": m.group(1), "title": m.group(2).strip()}
        return None

    @staticmethod
    def extract_article_info(text: str) -> Optional[Dict[str, str]]:
        """
        从一行文本中提取条款信息。

        支持格式：
        - "第一条 为了..."
        - "第1条 为了..."
        - "第三十五条　县级以上..."
        - "1.1 范围"
        - "3.1.2 定义"

        Returns:
            {"number": "一", "content": "为了..."} 或 None
        """
        patterns = [
            r"第([一二三四五六七八九十百零\d]+)条\s*(.*)",
            r"^(\d+\.\d+\.\d+)\s+(.*)",
            r"^(\d+\.\d+)\s+(.*)",
        ]
        for pattern in patterns:
            m = re.match(pattern, text.strip())
            if m:
                return {"number": m.group(1), "content": m.group(2).strip()}
        return None

    @staticmethod
    def compute_content_hash(text: str) -> str:
        """计算内容的 SHA256 哈希（用于去重和变更检测）。"""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def remove_watermark_text(text: str) -> str:
        """去除常见水印文字和页眉页脚干扰。"""
        watermarks = [
            r"国家标准全文公开系统",
            r"GB\s*\d+[\.-]\d+",
            r"第\s*\d+\s*页.*共\s*\d+\s*页",
            r"ICS\s*\d+\.\d+",
            r"版权所有.*侵权必究",
            r"—\s*\d+\s*—",
        ]
        for pat in watermarks:
            text = re.sub(pat, "", text, flags=re.IGNORECASE)
        return TextCleaner.normalize_text(text)

    @staticmethod
    def clean_law_text(text: str) -> str:
        """
        针对法律法规文本的专项清洗：
        - 保留"第X章""第X条"等结构信息
        - 去除不必要的空白
        - 统一全角标点
        """
        text = TextCleaner.normalize_text(text)
        # 全角标点统一（半角逗号 -> 全角逗号，在中文上下文中）
        # 这里保持原文不做强制转换，因为可能影响数字中的逗号
        return text


# ---------------------------------------------------------------------------
# 统一数据输出格式
# ---------------------------------------------------------------------------

class DataDumper:
    """
    将结构化数据以统一格式输出到 JSON 文件。

    输出目录结构：
        {output_dir}/{source_name}/YYYY-MM/YYYY-MM-DD_{doc_id}.json
    """

    def __init__(self, output_dir: str = "data/structured"):
        self.output_dir = Path(output_dir)

    def dump_law(self, doc: Dict[str, Any], source_name: str) -> str:
        """
        保存单条法规/标准数据。

        Args:
            doc: 符合 data_schema 的字典
            source_name: 数据源名称（如 "flk.npc.gov.cn"）

        Returns:
            保存的文件路径
        """
        doc_id = doc.get("id", uuid.uuid4().hex[:12])
        publish_date = doc.get("publish_date", datetime.now().strftime("%Y-%m"))
        year_month = publish_date[:7] if len(publish_date) >= 7 else "unknown"

        dir_path = self.output_dir / source_name / year_month
        dir_path.mkdir(parents=True, exist_ok=True)

        safe_date = publish_date.replace("/", "-") if publish_date else "unknown"
        filename = f"{safe_date}_{doc_id}.json"
        file_path = dir_path / filename

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)

        return str(file_path)

    def dump_batch(
        self,
        docs: List[Dict[str, Any]],
        source_name: str,
        batch_name: str = "",
    ) -> str:
        """
        批量保存为单个 JSONL 文件（每行一个 JSON 对象）。

        Args:
            docs: 文档列表
            source_name: 数据源名称
            batch_name: 批次名（可选）

        Returns:
            保存的文件路径
        """
        dir_path = self.output_dir / source_name / "_batches"
        dir_path.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = batch_name or f"batch_{timestamp}"
        file_path = dir_path / f"{name}.jsonl"

        with open(file_path, "w", encoding="utf-8") as f:
            for doc in docs:
                f.write(json.dumps(doc, ensure_ascii=False) + "\n")

        return str(file_path)


# ---------------------------------------------------------------------------
# 爬虫基类
# ---------------------------------------------------------------------------

class BaseCrawler(ABC):
    """
    所有数据源采集器的抽象基类。

    子类必须实现：
    - get_search_results(): 检索并返回结果列表
    - crawl_detail(): 抓取单条法规的详情
    - parse_detail(): 解析详情并返回结构化数据

    子类可选重写：
    - pre_crawl_hook(): 采集前置处理
    - post_crawl_hook(): 采集后置处理

    使用示例：
        class FlkCrawler(BaseCrawler):
            source_name = "flk.npc.gov.cn"
            source_url = "https://flk.npc.gov.cn"

            def get_search_results(self, keyword, **kwargs):
                # 实现检索逻辑
                pass

            def crawl_detail(self, item):
                # 实现详情抓取
                pass

            def parse_detail(self, raw_data, item):
                # 实现解析逻辑
                pass

        crawler = FlkCrawler(data_dir="data")
        crawler.run(keyword="消防")
    """

    # --- 子类必须定义的属性 ---
    source_name: str = ""           # 数据源名称，如 "flk.npc.gov.cn"
    source_url: str = ""            # 数据源首页 URL
    source_type: str = "generic"    # 数据源类型：law_api / gb_standard / gov_website / local_gov

    def __init__(
        self,
        data_dir: str = "data",
        log_dir: str = "logs",
        checkpoint_dir: str = "checkpoints",
        request_interval: float = DEFAULT_REQUEST_INTERVAL,
        max_retries: int = DEFAULT_RETRY_COUNT,
        user_agent: Optional[str] = None,
        proxy: Optional[str] = None,
    ):
        """
        Args:
            data_dir: 数据存储根目录
            log_dir: 日志目录
            checkpoint_dir: 断点续传目录
            request_interval: 请求间隔（秒）
            max_retries: 最大重试次数
            user_agent: 自定义 User-Agent
            proxy: HTTP 代理地址
        """
        self.data_dir = Path(data_dir)
        self.log_dir = Path(log_dir)
        self.checkpoint_dir = Path(checkpoint_dir)

        # 确保目录存在
        for d in [self.data_dir, self.log_dir, self.checkpoint_dir]:
            d.mkdir(parents=True, exist_ok=True)

        # 核心组件
        self.rate_limiter = RateLimiter(interval=request_interval)
        self.retry_handler = RetryHandler(max_retries=max_retries)
        self.request_manager = RequestManager(
            user_agent=user_agent or DEFAULT_USER_AGENT,
            rate_limiter=self.rate_limiter,
            proxy=proxy,
        )
        self.logger = CrawlLogger(
            name=self.source_name.replace(".", "_") if self.source_name else "base",
            log_dir=str(self.log_dir),
        )
        self.checkpoint = CheckpointManager(
            checkpoint_path=str(
                self.checkpoint_dir / f"{self.source_name.replace('.', '_')}.json"
            ),
            crawler_name=self.__class__.__name__,
            source=self.source_name,
        )
        self.text_cleaner = TextCleaner()
        self.data_dumper = DataDumper(
            output_dir=str(self.data_dir / "structured")
        )

        # 统计
        self.stats: Dict[str, int] = {"success": 0, "failed": 0, "skipped": 0}

    # ------------------------------------------------------------------
    # 子类必须实现的抽象方法
    # ------------------------------------------------------------------

    @abstractmethod
    def get_search_results(
        self, keyword: str, **kwargs
    ) -> List[Dict[str, Any]]:
        """
        根据关键字检索并返回结果列表。

        Args:
            keyword: 搜索关键字（如 "消防"）

        Returns:
            结果列表，每项包含至少 {"id": ..., "title": ..., "url": ...}
        """
        ...

    @abstractmethod
    def crawl_detail(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """
        抓取单条法规/标准的详情和正文。

        Args:
            item: get_search_results 返回的单个条目

        Returns:
            包含原始数据（HTML/JSON/二进制）的字典，用于 parse_detail
        """
        ...

    @abstractmethod
    def parse_detail(
        self, raw_data: Dict[str, Any], item: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        解析原始数据，输出符合统一 Schema 的结构化数据。

        Args:
            raw_data: crawl_detail 返回的原始数据
            item: get_search_results 返回的原始条目信息

        Returns:
            符合 data_schema 的结构化字典
        """
        ...

    # ------------------------------------------------------------------
    # 可选钩子
    # ------------------------------------------------------------------

    def pre_crawl_hook(self, **kwargs) -> bool:
        """
        采集前置钩子。返回 False 可中止采集。
        可用于预检查、清理临时文件等。
        """
        return True

    def post_crawl_hook(self, **kwargs):
        """采集后置钩子。可用于生成统计报告、清理等。"""
        self.logger.log_stat(
            self.source_name, "crawl_complete",
            value=self.stats,
            total_items=self.stats["success"] + self.stats["failed"] + self.stats["skipped"],
        )

    def should_skip(self, item: Dict[str, Any]) -> bool:
        """
        判断是否跳过某个条目（如已采集、已排除）。

        默认根据 checkpoint 中的 completed_ids 判断。
        子类可重写加入更多逻辑。
        """
        item_id = item.get("id", "")
        completed = self.checkpoint.load().get("completed_ids", [])
        return item_id in completed

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def run(
        self,
        keyword: str = "消防",
        max_items: Optional[int] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        """
        执行完整的采集流程。

        Args:
            keyword: 搜索关键字
            max_items: 最大采集条目数（None 表示全量）
            **kwargs: 传递给各子方法的附加参数

        Returns:
            所有成功采集的结构化文档列表
        """
        self.logger.log_stat(self.source_name, "crawl_start", {
            "keyword": keyword,
            "max_items": max_items,
        })

        # 前置钩子
        if not self.pre_crawl_hook(**kwargs):
            self.logger.log_stat(self.source_name, "crawl_aborted", "pre_crawl_hook returned False")
            return []

        # 检索
        search_results = self.get_search_results(keyword, **kwargs)
        self.logger.log_stat(
            self.source_name, "search_complete",
            value=len(search_results),
        )

        # 过滤已采集
        pending_items = [
            item for item in search_results
            if not self.should_skip(item)
        ]
        self.logger.log_stat(
            self.source_name, "pending_items",
            value=len(pending_items),
            total=len(search_results),
        )

        if max_items:
            pending_items = pending_items[:max_items]

        # 逐条采集
        results: List[Dict[str, Any]] = []
        for idx, item in enumerate(pending_items):
            item_id = item.get("id", f"unknown_{idx}")
            item_title = item.get("title", "无标题")

            try:
                self.logger.log_stat(
                    self.source_name, "crawling_item",
                    {"index": idx + 1, "total": len(pending_items),
                     "id": item_id, "title": item_title},
                )

                # 采集详情
                raw_data = self.crawl_detail(item)

                # 解析为结构化数据
                doc = self.parse_detail(raw_data, item)

                # 保存
                output_path = self.data_dumper.dump_law(doc, self.source_name)
                self.logger.log_crawl(
                    url=item.get("url", ""),
                    status=200,
                    duration_ms=0,
                    doc_id=item_id,
                    title=item_title,
                    output=output_path,
                )

                # 标记完成
                self.checkpoint.mark_complete(item_id)
                self.stats["success"] += 1
                results.append(doc)

            except Exception as e:
                self.logger.log_error(
                    url=item.get("url", ""),
                    error=str(e),
                    item_id=item_id,
                    title=item_title,
                )
                self.checkpoint.mark_failed(item_id)
                self.stats["failed"] += 1
                continue

        # 后置钩子
        self.post_crawl_hook(results=results)

        return results

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _build_meta(
        self,
        doc_id: str,
        title: str,
        doc_type: str,
        publisher: str = "",
        publish_date: str = "",
        effective_date: str = "",
        status: str = "现行有效",
        source_url: str = "",
        hierarchy: str = "法律",
    ) -> Dict[str, Any]:
        """构建统一的元数据基础框架。"""
        return {
            "id": doc_id,
            "title": title,
            "doc_type": doc_type,
            "publisher": publisher,
            "publish_date": publish_date,
            "effective_date": effective_date,
            "status": status,
            "source_url": source_url or self.source_url,
            "source_name": self.source_name,
            "hierarchy": hierarchy,
            "chapters": [],
            "full_text": "",
            "crawl_time": datetime.now(timezone.utc).isoformat(),
            "content_hash": "",
        }

    def _make_doc_id(self, *parts: str) -> str:
        """基于组成部分生成唯一标识（SHA256 前 16 位）。"""
        raw = "|".join(parts)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _resolve_url(self, relative_url: str) -> str:
        """将相对 URL 解析为绝对 URL。"""
        if bool(urlparse(relative_url).netloc):
            return relative_url
        return urljoin(self.source_url, relative_url)

    def _strip_tags(self, html: str) -> str:
        """去除 HTML 标签，返回纯文本（委托给 TextCleaner）。"""
        return self.text_cleaner.clean_html(html)

    def _to_filename(self, title: str, ext: str = ".txt") -> str:
        """将标题转换为安全的文件名。"""
        safe = re.sub(r"[<>:\"/\\|?*]", "_", title)
        safe = re.sub(r"\s+", "_", safe)
        return f"{safe[:100]}{ext}"

    def close(self):
        """清理资源（关闭 HTTP Session）。"""
        self.request_manager.session.close()


# ---------------------------------------------------------------------------
# 命令行入口（便于测试基类功能）
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # 简单自测：验证各组件可正常初始化
    print("=== BaseCrawler 组件自测 ===")

    # 测试 Logger
    logger = CrawlLogger(name="test", log_dir="/tmp/fire_rag_test_logs")
    logger.log_crawl("http://example.com", 200, 123.45)
    logger.log_error("http://example.com", "Connection timeout")
    logger.log_stat("test_source", "test_stat", 42)
    print("  [OK] CrawlLogger")

    # 测试 RateLimiter
    limiter = RateLimiter(interval=0.1)
    t0 = time.monotonic()
    limiter.wait()
    limiter.wait()
    limiter.wait()
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.2, f"RateLimiter 间隔不足: {elapsed}"
    print("  [OK] RateLimiter")

    # 测试 CheckpointManager
    cpm = CheckpointManager(
        checkpoint_path="/tmp/fire_rag_test_checkpoint.json",
        crawler_name="TestCrawler",
        source="test.example.com",
    )
    cpm.save({"last_page": 3})
    data = cpm.load()
    assert data["last_page"] == 3
    cpm.mark_complete("item_001")
    cpm.mark_complete("item_002")
    cpm.mark_failed("item_003")
    pending = cpm.get_pending_ids(["item_001", "item_002", "item_003", "item_004"])
    assert pending == ["item_003", "item_004"], f"pending 应为 ['item_003','item_004']，实际为 {pending}"
    print("  [OK] CheckpointManager")

    # 测试 TextCleaner
    cleaner = TextCleaner()
    html = "<html><body><script>alert(1)</script><nav>Menu</nav><p>第一条  这是正文内容。</p></body></html>"
    cleaned = cleaner.clean_html(html)
    assert "alert" not in cleaned
    assert "正文内容" in cleaned
    assert "Menu" not in cleaned

    date_str = "发布日期：2022-03-15"
    extracted = cleaner.extract_date(date_str)
    assert extracted == "2022-03-15"

    hash_val = cleaner.compute_content_hash("测试内容")
    assert len(hash_val) == 64
    print("  [OK] TextCleaner")

    # 测试 DataDumper
    dumper = DataDumper(output_dir="/tmp/fire_rag_test_data")
    test_doc = {
        "id": "test_001",
        "title": "测试法规",
        "doc_type": "law",
        "publisher": "测试机关",
        "publish_date": "2026-06-01",
        "effective_date": "2026-06-01",
        "status": "现行有效",
        "source_url": "http://example.com/test",
        "source_name": "test.example.com",
        "hierarchy": "法律",
        "chapters": [
            {
                "chapter_title": "第一章 总则",
                "articles": [
                    {"article_no": "第一条", "content": "测试内容。"}
                ],
            }
        ],
        "full_text": "第一章 总则\n第一条 测试内容。",
        "crawl_time": datetime.now(timezone.utc).isoformat(),
        "content_hash": cleaner.compute_content_hash("测试内容"),
    }
    path = dumper.dump_law(test_doc, "test.example.com")
    assert os.path.exists(path)
    print(f"  [OK] DataDumper -> {path}")

    # 清理测试文件
    import shutil
    shutil.rmtree("/tmp/fire_rag_test_logs", ignore_errors=True)
    shutil.rmtree("/tmp/fire_rag_test_data", ignore_errors=True)
    Path("/tmp/fire_rag_test_checkpoint.json").unlink(missing_ok=True)

    print("\n=== 所有组件自测通过 ===")
