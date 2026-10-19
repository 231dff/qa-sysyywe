import json
import os
import sys
import re
import httpx
from datetime import datetime, timedelta, timezone

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from utils.guardrails import sanitize_text
except ImportError:
    def sanitize_text(text: str) -> str:
        if not text: return ""
        text = re.sub(r'(sk-[a-zA-Z0-9]{8})[a-zA-Z0-9]+', r'\1***', text)
        text = re.sub(r'[\w\.-]+@[\w\.-]+\.\w+', '[EMAIL]', text)
        text = re.sub(r'1[3-9]\d{9}', '[PHONE]', text)
        return text

def fetch_and_convert_traces(limit: int = 1000): # ★ 改成了 1000
    print(f"开始从 Langfuse (Observations API v2) 拉取最近 {limit} 条 Trace...")
    
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY")
    host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
    
    if not public_key or not secret_key:
        print("❌ 缺少 Langfuse Keys，请检查环境变量")
        return
        
    if host.endswith('/'): host = host[:-1]

    now = datetime.now(timezone.utc)
    from_time = now - timedelta(days=7)
    from_start_time = from_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    to_start_time = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    
    url = f"{host}/api/public/v2/observations?fromStartTime={from_start_time}&toStartTime={to_start_time}&limit={limit}"
    
    try:
        resp = httpx.get(url, auth=(public_key, secret_key), timeout=30.0) # 超时加长
        resp.raise_for_status()
        result = resp.json()
        traces = result.get("data", [])
        print(f"从 Langfuse 拿到了 {len(traces)} 条原始数据。")
    except Exception as e:
        print(f"❌ 连接 Langfuse API 失败: {e}")
        return

    reflow_cases = []
    for trace in traces:
        query = ""
        input_data = trace.get("input")
        if isinstance(input_data, dict):
            query = input_data.get("query", "")
        elif isinstance(input_data, str):
            query = input_data

        answer = ""
        output_data = trace.get("output")
        if isinstance(output_data, dict):
            answer = output_data.get("text", str(output_data))
        elif isinstance(output_data, str):
            answer = output_data

        # ★ 过滤条件调整：必须包含 query，且名字包含 chat/stream
        is_bad_case = False
        latency = trace.get("latency", 0) or 0
        name = trace.get("name", "")
        
        # 如果能提取到 query，说明是对话；如果对话耗时 > 5s 或者发生了异常，视为 Bad Case
        if query and (latency > 5.0 or "error" in str(output_data).lower()):
            is_bad_case = True
            
        # 专门抓对话
        if "chat" in name or "stream" in name:
            is_bad_case = True

        if is_bad_case and query:
            safe_query = sanitize_text(query)
            reflow_cases.append({
                "query": safe_query,
                "original_bad_answer": answer,
                "source_trace_id": trace.get("id", ""),
                "failure_reason": "slow_latency_or_error",
                "expected_ground_truth": "" 
            })

    os.makedirs("evals/datasets", exist_ok=True)
    output_path = "evals/datasets/reflow_cases.jsonl"
    
    with open(output_path, "w", encoding="utf-8") as f:
        for case in reflow_cases:
            f.write(json.dumps(case, ensure_ascii=False) + "\n")
            
    print(f"✅ 成功回流 {len(reflow_cases)} 条失败案例至 {output_path}")

if __name__ == "__main__":
    fetch_and_convert_traces()