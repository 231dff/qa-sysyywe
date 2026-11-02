"""
智能搜索助手 — 网页精读提取器 (DeepSeek 级清洗与截断)
"""
import httpx
import trafilatura
import asyncio
import logging
import time
import re
import tiktoken
from typing import AsyncGenerator

logger = logging.getLogger(__name__)

# 初始化 Token 计算器
try:
    _encoder = tiktoken.get_encoding("cl100k_base")
except Exception:
    _encoder = None

def count_tokens(text: str) -> int:
    if not text: return 0
    if _encoder:
        return len(_encoder.encode(text))
    return len(text) // 2

def smart_truncate_by_token(text: str, max_tokens: int = 2500) -> str:
    """
    DeepSeek 策略：如果超长，保留头部（75%）和尾部（25%），中间用提示词折叠。
    """
    if not text: return ""
    tokens = count_tokens(text)
    if tokens <= max_tokens:
        return text
    
    # 按比例截断字符
    head_chars = int(len(text) * 0.75)
    tail_chars = int(len(text) * 0.25)
    
    return (
        text[:head_chars] + 
        "\n\n...（因篇幅过长，中间部分已折叠。如果你需要更多细节，请更换更具体的搜索词重新检索）...\n\n" + 
        text[-tail_chars:]
    )

async def fetch_and_extract(url: str, client: httpx.AsyncClient) -> str:
    """抓取单个 URL 并提取正文"""
    try:
        resp = await client.get(
            url,
            timeout=httpx.Timeout(connect=3.0, read=4.0, write=4.0, pool=2.0),
            follow_redirects=True,
        )
        if resp.status_code != 200:
            return ""

        # DeepSeek 策略：开启 favor_precision（精准模式），过滤导航栏和页脚
        try:
            content = await asyncio.to_thread(
                trafilatura.extract, 
                resp.text, 
                include_comments=False, 
                include_tables=True, 
                favor_precision=True, # ★ 核心：高精度提取正文
                no_fallback=False
            )
        except Exception as e:
            logger.warning(f"[WebExtractor] trafilatura 解析崩溃 {url[:30]}: {e}")
            content = ""

        return content or ""
    except Exception as e:
        logger.warning(f"[WebExtractor] 抓取失败 {url[:30]}: {e}")
        return ""

async def stream_extract_contents(
    urls: list[str], time_budget: float = 3.0
) -> AsyncGenerator[tuple[str, str], None]:
    """流式抓取网页"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    start_time = time.time()
    async with httpx.AsyncClient(headers=headers, verify=False) as client:
        task_to_url: dict[asyncio.Task, str] = {}
        for url in urls:
            task = asyncio.create_task(fetch_and_extract(url, client))
            task_to_url[task] = url

        pending: set[asyncio.Task] = set(task_to_url.keys())
        try:
            while pending:
                elapsed = time.time() - start_time
                if elapsed >= time_budget:
                    break

                remaining = max(0.001, time_budget - elapsed)
                done, pending = await asyncio.wait(
                    pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    break

                for task in done:
                    url = task_to_url[task]
                    try:
                        content = task.result()
                    except Exception:
                        continue
                    if content and len(content) > 50:
                        yield (url, content)
        finally:
            for t in task_to_url:
                if not t.done():
                    t.cancel()
            if task_to_url:
                await asyncio.gather(*task_to_url.keys(), return_exceptions=True)

# ★ 之前漏掉的切片函数，现在补回来了！
def chunk_text(text: str, chunk_size: int = 600, overlap: int = 100) -> list[str]:
    """将长文切分为带有重叠的文本块"""
    chunks = []
    if not text:
        return chunks
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    current_chunk = ""
    for p in paragraphs:
        if len(current_chunk) + len(p) <= chunk_size:
            current_chunk += p + "\n"
        else:
            if current_chunk:
                chunks.append(current_chunk.strip())
            if len(p) > chunk_size:
                for i in range(0, len(p), chunk_size - overlap):
                    chunks.append(p[i : i + chunk_size])
                current_chunk = ""
            else:
                current_chunk = p + "\n"
    if current_chunk:
        chunks.append(current_chunk.strip())
    return chunks