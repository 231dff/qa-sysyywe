# 智能搜索助手 — 企业级评测框架
#
# 四类套件（离线回归 / 在线监控 / 故障注入 / 安全审计）通过统一的
# Suite 注册表与门禁机制协作，由 `python -m evals` 统一入口驱动。
#
# 各套件在导入时通过 `@register` 注册，运行入口（__main__.py）负责
# 按序执行并做整体门禁判定。
from evals.core import (
    KIND_OFFLINE,
    KIND_ONLINE,
    KIND_FAULT,
    KIND_SECURITY,
    KIND_QUALITY,
    KIND_MEMORY,
    KIND_LABELS,
    Assertion,
    CaseResult,
    SuiteResult,
    GateResult,
    Report,
    Suite,
    register,
    get_suite,
    list_suites,
    run_suites,
    export_json,
    export_html,
)
