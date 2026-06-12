#!/usr/bin/env python3
"""
GB 标准 OCR 处理脚本（原型）
==============================
用途：对 openstd.samr.gov.cn 在线预览页面的截图进行 OCR 文字识别。
      当前（2022年4月起）openstd 使用图片拼接模式渲染标准内容，
      本脚本通过 Playwright 自动截图各页，然后调用 PaddleOCR 进行识别。

重要法律提示：
  - 本标准 OCR 工具仅供个人学习、研究之用
  - 不得用于商业用途或传播
  - 工程建设类标准不在 openstd 收录范围，无法通过本脚本获取
  - 请遵守《中华人民共和国著作权法》及相关法规
  - 商业用途请通过 spc.org.cn 或 ndls.org.cn 采购正版

依赖安装：
  pip install playwright paddleocr pdf2image Pillow
  playwright install chromium
  # PaddleOCR 会自动下载模型文件

用法：
  # 方式1：自动截图+OCR（需要 openstd 预览 URL）
  python crawler_gb_ocr.py --url "http://c.gb688.cn/bzgk/gb/showGb?type=online&hcno=XXX" --output ./output/

  # 方式2：对已有截图进行批处理 OCR
  python crawler_gb_ocr.py --input-dir ./screenshots/ --output ./output/

  # 方式3：对单个图片进行 OCR
  python crawler_gb_ocr.py --image ./page_001.png --output ./output/

  # 方式4：从已有 PDF 进行 OCR
  python crawler_gb_ocr.py --pdf ./standard.pdf --output ./output/

作者：数据采集工程师
日期：2026-06-12
"""

import argparse
import base64
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PaddleOCR 封装
# ---------------------------------------------------------------------------

class OCREngine:
    """OCR 引擎封装，支持 PaddleOCR 和 Tesseract 两种后端。"""

    def __init__(self, engine: str = "paddleocr", use_gpu: bool = False):
        """
        初始化 OCR 引擎。

        Args:
            engine: OCR 引擎类型，"paddleocr" 或 "tesseract"
            use_gpu: 是否使用 GPU 加速
        """
        self.engine = engine
        self.use_gpu = use_gpu
        self._ocr = None
        self._init_engine()

    def _init_engine(self):
        """延迟初始化 OCR 引擎。"""
        if self.engine == "paddleocr":
            try:
                from paddleocr import PaddleOCR
                self._ocr = PaddleOCR(
                    use_angle_cls=True,   # 启用文字方向分类
                    lang="ch",            # 中英文混合
                    use_gpu=self.use_gpu,
                    show_log=False,
                    # PP-OCRv5 是目前最优的中文 OCR 模型
                    rec_model_dir=None,   # 使用默认模型
                    det_model_dir=None,
                )
                logger.info("PaddleOCR 引擎初始化完成 (PP-OCRv5)")
            except ImportError:
                logger.error("PaddleOCR 未安装。请执行: pip install paddleocr")
                sys.exit(1)
            except Exception as e:
                logger.error(f"PaddleOCR 初始化失败: {e}")
                logger.info("回退到 Tesseract 引擎...")
                self.engine = "tesseract"
                self._init_tesseract()

        elif self.engine == "tesseract":
            self._init_tesseract()
        else:
            raise ValueError(f"不支持的 OCR 引擎: {self.engine}")

    def _init_tesseract(self):
        """初始化 Tesseract 引擎。"""
        try:
            import pytesseract
            self._pytesseract = pytesseract
            # 检查 Tesseract 是否可用
            version = pytesseract.get_tesseract_version()
            logger.info(f"Tesseract 引擎初始化完成 (版本 {version})")
        except ImportError:
            logger.error("pytesseract 未安装。请执行: pip install pytesseract")
            logger.error("还需要安装 Tesseract 系统包: apt-get install tesseract-ocr tesseract-ocr-chi-sim")
            sys.exit(1)

    def recognize(self, image_path: str) -> Tuple[str, List[dict]]:
        """
        对图片进行 OCR 识别。

        Args:
            image_path: 图片文件路径

        Returns:
            (full_text, details) - 完整文本和逐行详情
        """
        if self.engine == "paddleocr":
            return self._recognize_paddleocr(image_path)
        else:
            return self._recognize_tesseract(image_path)

    def _recognize_paddleocr(self, image_path: str) -> Tuple[str, List[dict]]:
        """PaddleOCR 识别。"""
        result = self._ocr.ocr(image_path, cls=True)

        if not result or not result[0]:
            logger.warning(f"PaddleOCR 未检测到文字: {image_path}")
            return "", []

        lines = []
        details = []
        for line_info in result[0]:
            box = line_info[0]          # [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
            text = line_info[1][0]       # 识别文本
            confidence = line_info[1][1] # 置信度

            lines.append(text)
            details.append({
                "text": text,
                "confidence": round(confidence, 4),
                "box": [[round(p[0]), round(p[1])] for p in box],
            })

        full_text = "\n".join(lines)
        return full_text, details

    def _recognize_tesseract(self, image_path: str) -> Tuple[str, List[dict]]:
        """Tesseract 识别（后备方案）。"""
        from PIL import Image

        img = Image.open(image_path)

        # 使用中英文混合识别
        text = self._pytesseract.image_to_string(
            img, lang="chi_sim+eng",
            config="--psm 6"  # 假设统一文本块
        )

        # Tesseract 不提供逐行置信度
        details = [{"text": line, "confidence": None} for line in text.split("\n") if line.strip()]
        return text, details

    def recognize_table(self, image_path: str) -> Optional[str]:
        """
        使用 PP-Structure 进行表格识别。
        仅在 PaddleOCR 引擎下可用。

        Returns:
            HTML 格式的表格，或 None
        """
        if self.engine != "paddleocr":
            logger.warning("表格识别仅支持 PaddleOCR 引擎")
            return None

        try:
            from paddleocr import PPStructure
            engine = PPStructure(show_log=False, lang='ch')
            result = engine(image_path)

            html_parts = []
            for item in result:
                if item['type'] == 'table':
                    html_parts.append(item['res']['html'])
            return "\n\n".join(html_parts) if html_parts else None
        except ImportError:
            logger.warning("PPStructure 不可用，表格识别跳过")
            return None
        except Exception as e:
            logger.error(f"表格识别失败: {e}")
            return None


# ---------------------------------------------------------------------------
# 截图获取
# ---------------------------------------------------------------------------

class StandardPageCapture:
    """通过 Playwright 自动截取 openstd 预览页面的每一页。"""

    def __init__(self, headless: bool = True):
        self.headless = headless
        self.browser = None
        self.context = None
        self.page = None

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *args):
        await self.stop()

    async def start(self):
        """启动浏览器。"""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.error("playwright 未安装。请执行: pip install playwright && playwright install chromium")
            sys.exit(1)

        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-web-security",
            ],
        )
        self.context = await self.browser.new_context(
            viewport={"width": 1440, "height": 2000},
            device_scale_factor=2,  # 2x 分辨率提高 OCR 质量
        )
        self.page = await self.context.new_page()
        logger.info("Playwright 浏览器已启动")
        return self

    async def stop(self):
        """关闭浏览器。"""
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if hasattr(self, "_pw") and self._pw:
            await self._pw.stop()
        logger.info("浏览器已关闭")

    async def capture_from_url(
        self,
        preview_url: str,
        output_dir: str,
        max_pages: int = 500,
        page_wait: float = 2.0,
    ) -> List[str]:
        """
        从 openstd 预览 URL 自动截取所有页面。

        Args:
            preview_url: openstd 在线预览页面 URL
            output_dir: 输出目录
            max_pages: 最大页数限制
            page_wait: 每页加载等待时间（秒）

        Returns:
            截图文件路径列表
        """
        os.makedirs(output_dir, exist_ok=True)
        logger.info(f"开始访问预览页面: {preview_url}")

        await self.page.goto(preview_url, wait_until="networkidle", timeout=30000)
        await self.page.wait_for_timeout(3000)  # 等待 JS 渲染

        # 尝试获取总页数
        total_pages = await self._get_total_pages()
        if total_pages:
            logger.info(f"检测到总页数: {total_pages}")
        else:
            logger.warning("无法获取总页数，将按最大 {max_pages} 页处理")
            total_pages = max_pages

        screenshots = []
        for page_num in range(1, min(total_pages, max_pages) + 1):
            try:
                # 尝试滚动到指定页面
                await self._goto_page(page_num)
                await self.page.wait_for_timeout(int(page_wait * 1000))

                # 截图当前可见内容
                filename = f"page_{page_num:04d}.png"
                filepath = os.path.join(output_dir, filename)

                # 截取内容区域
                try:
                    content_element = await self.page.wait_for_selector(
                        "canvas, img[id^=canvas_], .page, #pageContainer, .pdfViewer",
                        timeout=5000,
                    )
                    if content_element:
                        await content_element.screenshot(path=filepath)
                    else:
                        await self.page.screenshot(path=filepath, full_page=False)
                except Exception:
                    # 找不到内容元素，截取整个可视区域
                    await self.page.screenshot(path=filepath, full_page=False)

                screenshots.append(filepath)
                logger.info(f"第 {page_num}/{total_pages} 页截图完成: {filepath}")

            except Exception as e:
                logger.error(f"第 {page_num} 页截图失败: {e}")
                continue

        logger.info(f"共截取 {len(screenshots)} 页")
        return screenshots

    async def _get_total_pages(self) -> Optional[int]:
        """尝试获取总页数。"""
        try:
            # 常见的选择器模式
            selectors = [
                "#numPages",
                ".totalPages",
                "#pageNumberContainer span:last-child",
                '[data-total-pages]',
                "input#pageNumber",
            ]
            for selector in selectors:
                try:
                    el = await self.page.wait_for_selector(selector, timeout=1000)
                    if el:
                        text = await el.text_content() or await el.get_attribute("value")
                        if text:
                            # 提取数字
                            match = re.search(r"(\d+)", text)
                            if match:
                                return int(match.group(1))
                except Exception:
                    continue

            # 通过 JS 获取
            total = await self.page.evaluate("""
                () => {
                    const selectors = ['#numPages', '.totalPages', '.pageCount'];
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el && el.textContent) {
                            const m = el.textContent.match(/(\\d+)/);
                            if (m) return parseInt(m[1]);
                        }
                    }
                    // 尝试获取 canvas 数量
                    const canvases = document.querySelectorAll('canvas[id^=canvas_]');
                    if (canvases.length > 0) return canvases.length;
                    return null;
                }
            """)
            return total
        except Exception:
            return None

    async def _goto_page(self, page_num: int):
        """导航到指定页码。"""
        # 尝试通过输入框翻页
        try:
            input_selectors = [
                "input#pageNumber",
                "input.pageNumber",
                ".pageNumberInput",
                'input[type="number"]',
            ]
            for selector in input_selectors:
                try:
                    input_el = await self.page.wait_for_selector(selector, timeout=500)
                    if input_el:
                        await input_el.click()
                        await input_el.fill("")
                        await input_el.type(str(page_num), delay=50)
                        await self.page.keyboard.press("Enter")
                        return
                except Exception:
                    continue
        except Exception:
            pass

        # 备选：模拟向下滚动（估算）
        await self.page.evaluate(f"window.scrollTo(0, {page_num * 900})")


# ---------------------------------------------------------------------------
# 批处理
# ---------------------------------------------------------------------------

def process_images(
    input_dir: str,
    output_dir: str,
    engine: str = "paddleocr",
    use_gpu: bool = False,
    extract_tables: bool = True,
) -> str:
    """
    批处理目录中的所有图片。

    Args:
        input_dir: 输入图片目录
        output_dir: 输出目录
        engine: OCR 引擎类型
        use_gpu: 是否使用 GPU
        extract_tables: 是否提取表格

    Returns:
        合并后的完整文本
    """
    os.makedirs(output_dir, exist_ok=True)

    # 按文件名排序
    image_files = sorted(
        [f for f in os.listdir(input_dir) if f.lower().endswith((".png", ".jpg", ".jpeg", ".bmp", ".tiff"))]
    )

    if not image_files:
        logger.error(f"未找到图片文件: {input_dir}")
        return ""

    logger.info(f"开始处理 {len(image_files)} 张图片")

    ocr = OCREngine(engine=engine, use_gpu=use_gpu)
    all_text_parts = []
    all_details = {}

    for i, filename in enumerate(image_files):
        image_path = os.path.join(input_dir, filename)
        logger.info(f"OCR 识别: [{i+1}/{len(image_files)}] {filename}")

        try:
            text, details = ocr.recognize(image_path)

            # 保存单页文本
            base_name = os.path.splitext(filename)[0]
            text_file = os.path.join(output_dir, f"{base_name}.txt")
            with open(text_file, "w", encoding="utf-8") as f:
                f.write(text)

            # 保存详细结果（含坐标和置信度）
            json_file = os.path.join(output_dir, f"{base_name}.json")
            with open(json_file, "w", encoding="utf-8") as f:
                json.dump(details, f, ensure_ascii=False, indent=2)

            all_text_parts.append(f"=== 第 {i+1} 页 ===\n{text}")
            all_details[filename] = details

            # 表格提取
            if extract_tables and engine == "paddleocr":
                try:
                    table_html = ocr.recognize_table(image_path)
                    if table_html:
                        table_file = os.path.join(output_dir, f"{base_name}_table.html")
                        with open(table_file, "w", encoding="utf-8") as f:
                            f.write(f"<html><body>\n{table_html}\n</body></html>")
                        logger.info(f"  表格已提取: {table_file}")
                except Exception as e:
                    logger.warning(f"  表格提取失败: {e}")

        except Exception as e:
            logger.error(f"OCR 失败 [{filename}]: {e}")
            all_text_parts.append(f"=== 第 {i+1} 页 ===\n[OCR 识别失败: {e}]")

    # 合并所有文本
    full_text = "\n\n".join(all_text_parts)
    merged_file = os.path.join(output_dir, "_full_text.txt")
    with open(merged_file, "w", encoding="utf-8") as f:
        f.write(full_text)
    logger.info(f"合并文本已保存: {merged_file}")

    # 生成统计报告
    stats = _generate_stats(all_details)
    stats_file = os.path.join(output_dir, "_ocr_stats.json")
    with open(stats_file, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    logger.info(f"统计报告已保存: {stats_file}")
    _print_stats(stats)

    return full_text


def process_pdf(
    pdf_path: str,
    output_dir: str,
    engine: str = "paddleocr",
    use_gpu: bool = False,
    dpi: int = 300,
) -> str:
    """
    从 PDF 文件中提取页面并进行 OCR。
    当 PDF 为扫描件（图片型 PDF）时特别有用。

    Args:
        pdf_path: PDF 文件路径
        output_dir: 输出目录
        engine: OCR 引擎类型
        use_gpu: 是否使用 GPU
        dpi: 渲染 DPI

    Returns:
        合并后的完整文本
    """
    os.makedirs(output_dir, exist_ok=True)

    try:
        from pdf2image import convert_from_path
    except ImportError:
        logger.error("pdf2image 未安装。请执行: pip install pdf2image")
        logger.error("还需要安装 poppler: apt-get install poppler-utils")
        sys.exit(1)

    logger.info(f"正在将 PDF 转换为图片: {pdf_path}")
    images = convert_from_path(pdf_path, dpi=dpi)
    logger.info(f"PDF 共 {len(images)} 页")

    # 保存为临时图片
    temp_dir = os.path.join(output_dir, "_temp_pages")
    os.makedirs(temp_dir, exist_ok=True)

    for i, img in enumerate(images):
        img_path = os.path.join(temp_dir, f"page_{i+1:04d}.png")
        img.save(img_path, "PNG")
        logger.info(f"保存第 {i+1} 页图片: {img_path}")

    # 批处理 OCR
    full_text = process_images(
        input_dir=temp_dir,
        output_dir=output_dir,
        engine=engine,
        use_gpu=use_gpu,
    )

    return full_text


def _generate_stats(all_details: dict) -> dict:
    """生成 OCR 统计报告。"""
    total_chars = 0
    total_words = 0
    total_lines = 0
    confidences = []
    page_stats = {}

    for filename, details in all_details.items():
        page_chars = sum(len(d["text"]) for d in details)
        page_lines = len(details)
        page_confs = [d["confidence"] for d in details if d["confidence"] is not None]

        total_chars += page_chars
        total_lines += page_lines
        confidences.extend(page_confs)

        page_stats[filename] = {
            "characters": page_chars,
            "lines": page_lines,
            "avg_confidence": round(sum(page_confs) / len(page_confs), 4) if page_confs else None,
        }

    return {
        "total_pages": len(all_details),
        "total_characters": total_chars,
        "total_lines": total_lines,
        "overall_avg_confidence": round(sum(confidences) / len(confidences), 4) if confidences else None,
        "per_page": page_stats,
    }


def _print_stats(stats: dict):
    """打印统计信息。"""
    print("\n" + "=" * 60)
    print("OCR 统计报告")
    print("=" * 60)
    print(f"总页数:       {stats['total_pages']}")
    print(f"总字符数:     {stats['total_characters']:,}")
    print(f"总行数:       {stats['total_lines']}")
    print(f"平均置信度:   {stats['overall_avg_confidence']}")
    print("-" * 60)

    # 低置信度页面告警
    low_conf_pages = []
    for filename, info in stats["per_page"].items():
        if info["avg_confidence"] is not None and info["avg_confidence"] < 0.85:
            low_conf_pages.append((filename, info["avg_confidence"]))

    if low_conf_pages:
        print(f"低置信度页面 (<85%): {len(low_conf_pages)} 页")
        for filename, conf in low_conf_pages[:10]:
            print(f"  - {filename}: {conf:.2%}")
        if len(low_conf_pages) > 10:
            print(f"  ... 及其他 {len(low_conf_pages) - 10} 页")
    print("=" * 60)


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------

async def async_main(args):
    """异步主函数：处理 URL 截图模式。"""
    capture = StandardPageCapture(headless=args.headless)
    async with capture as cap:
        screenshots = await cap.capture_from_url(
            preview_url=args.url,
            output_dir=os.path.join(args.output, "screenshots"),
            max_pages=args.max_pages,
            page_wait=args.page_wait,
        )

    if screenshots:
        logger.info(f"截图完成，共 {len(screenshots)} 页，开始 OCR...")
        process_images(
            input_dir=os.path.join(args.output, "screenshots"),
            output_dir=os.path.join(args.output, "ocr_results"),
            engine=args.engine,
            use_gpu=args.gpu,
            extract_tables=not args.no_table,
        )
    else:
        logger.error("未获取到任何截图")


def main():
    parser = argparse.ArgumentParser(
        description="GB 标准 OCR 处理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 从 openstd 预览 URL 自动截图+OCR
  python crawler_gb_ocr.py --url "http://c.gb688.cn/bzgk/gb/showGb?type=online&hcno=XXX" -o ./output/

  # 批处理已有截图
  python crawler_gb_ocr.py --input-dir ./screenshots/ -o ./output/

  # 从扫描件 PDF OCR
  python crawler_gb_ocr.py --pdf ./standard.pdf -o ./output/
        """,
    )

    # 输入源（三选一）
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--url", type=str, help="openstd 在线预览页面 URL")
    input_group.add_argument("--input-dir", type=str, help="已有截图目录（批处理模式）")
    input_group.add_argument("--image", type=str, help="单张图片路径")
    input_group.add_argument("--pdf", type=str, help="PDF 文件路径")

    # 输出
    parser.add_argument("-o", "--output", type=str, default="./ocr_output",
                        help="输出目录 (默认: ./ocr_output)")

    # OCR 引擎选项
    parser.add_argument("--engine", type=str, default="paddleocr",
                        choices=["paddleocr", "tesseract"],
                        help="OCR 引擎 (默认: paddleocr)")
    parser.add_argument("--gpu", action="store_true",
                        help="启用 GPU 加速")
    parser.add_argument("--no-table", action="store_true",
                        help="禁用表格识别")

    # 截图选项
    parser.add_argument("--headless", action="store_true", default=True,
                        help="无头模式运行浏览器 (默认: True)")
    parser.add_argument("--no-headless", dest="headless", action="store_false",
                        help="显示浏览器窗口")
    parser.add_argument("--max-pages", type=int, default=500,
                        help="最大截图页数 (默认: 500)")
    parser.add_argument("--page-wait", type=float, default=2.0,
                        help="每页加载等待秒数 (默认: 2.0)")

    # PDF 选项
    parser.add_argument("--dpi", type=int, default=300,
                        help="PDF 转图片 DPI (默认: 300)")

    args = parser.parse_args()

    # 检查 PaddleOCR 可用性
    if args.engine == "paddleocr":
        try:
            import paddleocr  # noqa: F401
        except ImportError:
            logger.warning("PaddleOCR 未安装，将使用 Tesseract。安装: pip install paddleocr")
            args.engine = "tesseract"

    os.makedirs(args.output, exist_ok=True)

    # 分发处理
    if args.url:
        # 异步模式：URL 截图
        import asyncio
        asyncio.run(async_main(args))

    elif args.input_dir:
        # 批处理已有截图
        if not os.path.isdir(args.input_dir):
            logger.error(f"输入目录不存在: {args.input_dir}")
            sys.exit(1)
        process_images(
            input_dir=args.input_dir,
            output_dir=args.output,
            engine=args.engine,
            use_gpu=args.gpu,
            extract_tables=not args.no_table,
        )

    elif args.image:
        # 单张图片
        if not os.path.isfile(args.image):
            logger.error(f"图片文件不存在: {args.image}")
            sys.exit(1)

        ocr = OCREngine(engine=args.engine, use_gpu=args.gpu)
        text, details = ocr.recognize(args.image)

        base_name = os.path.splitext(os.path.basename(args.image))[0]
        text_file = os.path.join(args.output, f"{base_name}.txt")
        json_file = os.path.join(args.output, f"{base_name}.json")

        with open(text_file, "w", encoding="utf-8") as f:
            f.write(text)
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(details, f, ensure_ascii=False, indent=2)

        print(text)
        logger.info(f"结果已保存: {text_file}, {json_file}")

    elif args.pdf:
        # PDF 处理
        if not os.path.isfile(args.pdf):
            logger.error(f"PDF 文件不存在: {args.pdf}")
            sys.exit(1)
        process_pdf(
            pdf_path=args.pdf,
            output_dir=args.output,
            engine=args.engine,
            use_gpu=args.gpu,
            dpi=args.dpi,
        )

    logger.info("处理完成。")


if __name__ == "__main__":
    main()
