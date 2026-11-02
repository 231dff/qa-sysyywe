"""
智能搜索助手 — Prometheus 指标收集与可用性统计

★ 修 23：使用真正的独立 CollectorRegistry，
   不再注释说“独立”实际复用全局 REGISTRY。
   /api/metrics 通过 generate_latest(_METRICS_REGISTRY) 手动导出，不受影响。
"""
from __future__ import annotations

import time
import threading
from collections import deque
from dataclasses import dataclass

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    CollectorRegistry,
    generate_latest,
)

# ★ 修 23：真正的独立注册表
_METRICS_REGISTRY: CollectorRegistry = CollectorRegistry()

# ============================================================
# Prometheus 指标定义
# ============================================================
http_requests_total = Counter(
    "http_requests_total", "HTTP 请求总数",
    ["method", "path", "status_code"],
    registry=_METRICS_REGISTRY,
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds", "HTTP 请求延迟 (秒)",
    ["method", "path"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
    registry=_METRICS_REGISTRY,
)

sse_streams_total = Counter(
    "sse_streams_total", "SSE 流式连接总数",
    ["status"],
    registry=_METRICS_REGISTRY,
)

search_requests_total = Counter(
    "search_requests_total", "搜索 API 调用总数",
    ["provider", "status"],
    registry=_METRICS_REGISTRY,
)

tokens_consumed_total = Counter(
    "tokens_consumed_total", "LLM Token 消耗总数",
    ["type"],
    registry=_METRICS_REGISTRY,
)

active_sessions_gauge = Gauge(
    "active_sessions", "当前活跃会话数",
    registry=_METRICS_REGISTRY,
)

search_duration_seconds = Histogram(
    "search_duration_seconds", "搜索调用延迟 (秒)",
    ["provider"],
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 15.0, 30.0],
    registry=_METRICS_REGISTRY,
)

llm_call_duration_seconds = Histogram(
    "llm_call_duration_seconds", "LLM 调用延迟 (秒)",
    ["operation"],
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 40.0, 60.0],
    registry=_METRICS_REGISTRY,
)

errors_total = Counter(
    "errors_total", "错误总数",
    ["type"],
    registry=_METRICS_REGISTRY,
)

extraction_total = Counter(
    "extraction_total", "知识图谱实体抽取调用总数",
    ["status"],
    registry=_METRICS_REGISTRY,
)

graph_entities = Gauge(
    "graph_entities", "知识图谱当前实体数",
    registry=_METRICS_REGISTRY,
)
graph_relations = Gauge(
    "graph_relations", "知识图谱当前关系数",
    registry=_METRICS_REGISTRY,
)

graph_search_duration_seconds = Histogram(
    "graph_search_duration_seconds", "知识图谱检索延迟 (秒)",
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
    registry=_METRICS_REGISTRY,
)


# ============================================================
# 滑动窗口可用性统计 (95% SLA)
# ============================================================
@dataclass
class _WindowBucket:
    timestamp: float
    successes: int = 0
    failures: int = 0


class AvailabilityTracker:
    def __init__(self, window_seconds: int = 300) -> None:
        self._window_seconds = window_seconds
        self._buckets: deque[_WindowBucket] = deque()
        self._lock = threading.Lock()

        self._availability_gauge = Gauge(
            "http_availability_ratio", "HTTP 可用性比率 (滑动窗口)",
            ["window"],
            registry=_METRICS_REGISTRY,
        )
        self._sla_violation_gauge = Gauge(
            "http_sla_violation", "可用性 SLA 违规 (1 = 低于 95% 阈值)",
            ["window"],
            registry=_METRICS_REGISTRY,
        )

    def record_success(self) -> None:
        self._record(1, 0)

    def record_failure(self) -> None:
        self._record(0, 1)

    def _record(self, successes: int, failures: int) -> None:
        now = time.time()
        with self._lock:
            cutoff = now - self._window_seconds
            while self._buckets and self._buckets[0].timestamp < cutoff:
                self._buckets.popleft()

            current_second = int(now)
            if self._buckets and int(self._buckets[-1].timestamp) == current_second:
                self._buckets[-1].successes += successes
                self._buckets[-1].failures += failures
            else:
                self._buckets.append(
                    _WindowBucket(timestamp=now, successes=successes, failures=failures)
                )

    def _compute_availability(self, window_seconds: int) -> tuple[float, int, int]:
        now = time.time()
        cutoff = now - window_seconds
        total_s = 0
        total_f = 0
        with self._lock:
            for bucket in self._buckets:
                if bucket.timestamp >= cutoff:
                    total_s += bucket.successes
                    total_f += bucket.failures
        total = total_s + total_f
        ratio = total_s / total if total > 0 else 1.0
        return ratio, total_s, total_f

    def availability_ratio(self, window_seconds: int | None = None) -> float:
        w = window_seconds or self._window_seconds
        ratio, _, _ = self._compute_availability(w)
        return ratio

    def meets_sla(self, threshold: float = 0.95) -> bool:
        return self.availability_ratio() >= threshold

    @property
    def stats(self) -> dict:
        windows = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}
        result: dict[str, dict] = {}
        for label, secs in windows.items():
            ratio, successes, failures = self._compute_availability(secs)
            total = successes + failures
            result[label] = {
                "availability_pct": round(ratio * 100, 2),
                "successes": successes,
                "failures": failures,
                "total": total,
                "meets_95_sla": ratio >= 0.95,
            }
        return result

    def update_prometheus_gauges(self) -> None:
        windows = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}
        for label, secs in windows.items():
            ratio, _, _ = self._compute_availability(secs)
            self._availability_gauge.labels(window=label).set(ratio)
            self._sla_violation_gauge.labels(window=label).set(
                1.0 if ratio < 0.95 else 0.0
            )


_availability_tracker: AvailabilityTracker | None = None


def get_availability_tracker() -> AvailabilityTracker:
    global _availability_tracker
    if _availability_tracker is None:
        _availability_tracker = AvailabilityTracker(window_seconds=300)
    return _availability_tracker


# ============================================================
# 便捷记录函数
# ============================================================
def record_http_request(method: str, path: str, status_code: int, duration: float, is_error: bool = False) -> None:
    http_requests_total.labels(method=method, path=path, status_code=str(status_code)).inc()
    http_request_duration_seconds.labels(method=method, path=path).observe(duration)

    tracker = get_availability_tracker()
    if is_error or status_code >= 500:
        tracker.record_failure()
    else:
        tracker.record_success()


def record_sse_stream(status: str) -> None:
    sse_streams_total.labels(status=status).inc()


def record_search(provider: str, status: str, duration: float) -> None:
    search_requests_total.labels(provider=provider, status=status).inc()
    search_duration_seconds.labels(provider=provider).observe(duration)


def record_tokens(input_tokens: int, output_tokens: int) -> None:
    if input_tokens > 0:
        tokens_consumed_total.labels(type="input").inc(input_tokens)
    if output_tokens > 0:
        tokens_consumed_total.labels(type="output").inc(output_tokens)


def record_llm_call(operation: str, duration: float) -> None:
    llm_call_duration_seconds.labels(operation=operation).observe(duration)


def record_error(error_type: str) -> None:
    errors_total.labels(type=error_type).inc()


def record_extraction(status: str) -> None:
    extraction_total.labels(status=status).inc()


def update_graph_size(entities: int, relations: int) -> None:
    graph_entities.set(entities)
    graph_relations.set(relations)


def record_graph_search(duration: float) -> None:
    graph_search_duration_seconds.observe(duration)


def update_active_sessions(count: int) -> None:
    active_sessions_gauge.set(count)


# ============================================================
# Prometheus 导出
# ============================================================
def get_metrics_text() -> str:
    get_availability_tracker().update_prometheus_gauges()
    return generate_latest(_METRICS_REGISTRY).decode("utf-8")


def get_availability_stats() -> dict:
    return get_availability_tracker().stats


def get_all_stats() -> dict:
    availability = get_availability_stats()
    metrics_snapshot: dict[str, dict] = {}

    for metric_name in [
        "http_requests_total",
        "sse_streams_total",
        "search_requests_total",
        "tokens_consumed_total",
        "errors_total",
    ]:
        metrics_snapshot[metric_name] = {}
        for metric in _METRICS_REGISTRY.collect():
            if metric.name == metric_name:
                for sample in metric.samples:
                    labels = sample.labels
                    label_str = ",".join(f"{k}={v}" for k, v in labels.items() if k != "window")
                    metrics_snapshot[metric_name][label_str] = sample.value

    return {
        "availability": availability,
        "prometheus_snapshot": metrics_snapshot,
    }