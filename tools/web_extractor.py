"""
智能搜索助手 — 网页精读提取器 (Web Extractor)
引入 DeepSeek 式的流式抓取与时间预算管理
"""
import httpx
import trafilatura
import asyncio
import logging
import time
from typing import AsyncGenerator

logger = logging.getLogger(__name__)

async def fetch_and_extract(url: str, client: httpx.AsyncClient) -> str:
    """抓取单个 URL 并提取正文"""
    try:
        resp = await client.get(
            url,
            timeout=httpx.Timeout(connect=3.0, read=4.0, write=4.0, pool=2.0),
            follow_redirects=True
        )
        if resp.status_code != 200:
            return ""
        
        # CPU 解析放入线程池
        content = await asyncio.to_thread(
            trafilatura.extract, resp.text, include_comments=False, include_tables=True, no_fallback=False
        )
        return content or ""
    except Exception as e:
        logger.warning(f"[WebExtractor] 抓取失败 {url[:30]}: {e}")
        return ""

async def stream_extract_contents(urls: list[str], time_budget: float = 6.0) -> AsyncGenerator[tuple[str, str], None]:
    """
    ★ 核心改造：流式抓取。
    只要哪个网页先返回且解析成功，就立刻 yield 出来，不再死等 gather。
    带有总时间预算（默认 6 秒），超出预算直接中断。
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    
    start_time = time.time()
    async with httpx.AsyncClient(headers=headers, verify=False) as client:
        # 创建所有抓取任务
        tasks = [asyncio.create_task(fetch_and_extract(url, client)) for url in urls]
        
        try:
            # ★ 核心机制：asyncio.as_completed 谁先完成，就先处理谁
            for coro in asyncio.as_completed(tasks):
                # 检查是否超出总时间预算
                if time.time() - start_time > time_budget:
                    logger.info(f"[WebExtractor] ⏰ 达到时间预算 {time_budget}s，提前中断抓取，仅使用已获取的内容。")
                    break
                
                # 等待这一轮最先完成的那个任务
                content = await coro
                # 找到这个任务对应的 URL（稍微 tricky，但为了逻辑清晰）
                # 实际上由于我们只关心内容，这里可以直接 yield 内容
                if content and len(content) > 50:
                    yield ("unknown", content)
        finally:
            # 无论如何，清理尚未完成的任务，防止资源泄漏
            for t in tasks:
                if not t.done():
                    t.cancel()

def chunk_text(text: str, chunk_size: int = 600, overlap: int = 100) -> list[str]:
    """将长文切分为带有重叠的文本块"""
    chunks = []
    if not text: return chunks
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    current_chunk = ""
    for p in paragraphs:
        if len(current_chunk) + len(p) <= chunk_size:
            current_chunk += p + "\n"
        else:
            if current_chunk: chunks.append(current_chunk.strip())
            if len(p) > chunk_size:
                for i in range(0, len(p), chunk_size - overlap):
                    chunks.append(p[i:i+chunk_size])
                current_chunk = ""
            else:
                current_chunk = p + "\n"
    if current_chunk: chunks.append(current_chunk.strip())
    return chunks