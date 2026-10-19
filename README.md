---
AIGC:
    Label: "1"
    ContentProducer: 001191110102MACQD9K64018705
    ProduceID: 7642208309482733858-data_volume/files/所有对话/主对话/qa-sysyywe_README_性能优化版.md
    ReservedCode1: ""
    ContentPropagator: 001191110102MACQD9K64028705
    PropagateID: 467726794233428#1792401587998
    ReservedCode2: ""
---
# 智能搜索助手 (qa-sysyywe)

**企业级自主决策搜索 Agent 系统** —— 基于 LangGraph (ReAct) + Tavily + FastAPI + Next.js 16，面向生产环境的实时联网问答平台。内置 **Proposer-Reviewer 双阶段幻觉核查**、**语义缓存（Semantic Cache）**、**并行工具调用**、**Agent 状态栏**、**动态模型热切换路由**，并沉淀了从可观测性到**生产轨迹回流（Data Flywheel）**的完整工程闭环。

---

## 定位

一个「按需取数、自主决策、自我纠错、极致极速」的搜索 Agent。LLM 依据工具契约自行决定**是否搜索、何时停止**，而非硬编码流水线。它同时打通三条检索通道（**实时搜索**、**RAG 知识库**、**知识图谱**），并以 `user_id` 贯穿跨会话记忆。

在工程层面，它不仅具备 **Harness 工程（约束、验证、纠正）** 的完整生产级系统，还针对大模型应用的延迟痛点进行了深度的**性能优化（Latency Optimization）**。

## 企业级能力总览

| 能力域 | 实现 | 说明 |
|--------|------|------|
| **自主决策** | LangGraph `create_react_agent` | 工具按需加载，`tools_condition` 驱动，信息充分即停 |
| **双阶段幻觉核查** | Proposer-Reviewer 架构 | 独立 Reviewer 交叉验证草稿，触发 `revise` 无痕重写 |
| **条件化极速审核** | 条件触发 + 极速模型路由 | 无检索证据时跳过 Reviewer；有证据时用轻量模型（如 qwen-turbo）秒级校验 |
| **语义缓存** | Redis + 本地向量哈希 | 新会话提问时进行相似度匹配，命中则极速流式返回（延迟降至 10ms 级） |
| **并行工具调用** | Prompt 强约束 | 引导模型在对比多实体（如手机对比）时，一次性输出多个 `tool_calls`，并发执行 |
| **Agent 状态栏** | 工具返回末尾注入 | 动态追加调用计数与预算，防止长任务迷失与死循环 |
| **长输出截断** | Context Engineering | 超长搜索落盘至 `/tmp`，通过 `read_tool_output` 按需读取，净化 KV Cache |
| **动态模型路由** | 前端设置面板 + 无状态实例 | 支持运行时动态覆盖 Provider、Base URL、API Key 和 Model，无缝热切换 |
| **生产数据飞轮** | Langfuse 轨迹回流 | 自动拉取 Bad Case 脱敏生成 Pytest 回归用例，形成评估闭环 |
| **全链路可观测** | OTel + Langfuse + Prometheus | Span 树覆盖 LLM/工具/Reviewer，失败态自动打标 |

## 核心特性与架构演进

### 1. 性能优化：从秒级到毫秒级的跨越

- **语义缓存（Semantic Cache）**：引入 Redis 存储历史问答对。在**新会话**首轮提问时，计算 Query 的字符 n-gram 哈希特征，若与历史提问余弦相似度 > 0.95，则直接截断 Agent 循环，以极速流式吐出缓存答案。
- **并行工具调用（Parallel Tool Calls）**：在 `System Prompt` 中强制要求：涉及独立子问题（如对比 A 和 B）时，必须在单次响应中输出多个 `tool_calls`。LangGraph 底层并发执行搜索，彻底打破串行网络 I/O 瓶颈。
- **条件化极速审核**：如果本轮 Agent 没有调用任何工具（纯常识问答），直接跳过 Reviewer；若需要审核，则强制路由至轻量级模型（如 `qwen-turbo`）执行 JSON 级判定，大幅缩短 TTFT（首字延迟）。

### 2. Harness 工程：Proposer-Reviewer 纠错闭环

普通 RAG 系统极易产生幻觉。本项目在 ReAct 主循环之外，引入了独立的 **Reviewer Agent**：

- **Proposer**（主 Agent）完成多轮检索并生成草稿。
- **Verifier**（Reviewer）拦截草稿，强制提取本轮所有 `ToolMessage` 作为“唯一事实证据”，逐句核查。
- 若发现幻觉或操作废话，输出 `REVISE` 并附带理由，触发主模型自我纠错。
- 前端通过 SSE 接收 `event: revise` 静默替换文本，用户全程无感。

### 3. 上下文工程（Context Engineering）与状态栏

- **Agent 状态栏（Budget-Aware）**：通过 Python `ContextVar` 维护当前请求的工具调用计数器。每次工具返回时，在末尾注入 `search: 2 次, read_tool_output: 1 次`，让 LLM 具备“步数预算意识”。
- **长文本落盘**：Tavily 搜索结果超过 3000 字符时强制落盘，只在上下文保留前 500 字摘要与文件路径，配合 `read_tool_output` 工具实现**渐进式披露（Progressive Disclosure）**，极大降低 Token 消耗。

### 4. 动态模型热切换（无状态实例化）

支持前端 `localStorage` 下发模型配置。后端的 `ReactAgent` 具备动态装配能力：

- 检测到前端覆盖配置时，现场实例化 `ChatOpenAI` 与 `create_react_agent`，实现**请求级线程安全隔离**。
- 彻底解决全局配置并发覆写的竞态问题。

## 架构总览

```text
┌──────────────┐  SSE Stream   ┌──────────────┐  Streaming POST  ┌────────────────┐
│ Next.js 16   │ ◄──────────── │ FastAPI 后端  │ ───────────────► │ LLM API        │
│ React 19 + TS│  event: token │ (port 8000)  │                  │ (OpenAI 兼容)   │
│ 设置面板动态  │  event: revise│              │ ───────────────► │ Tavily Search  │
│ 路由模型配置  │  event: progress│ LangGraph   │  Search API      │ API            │
└──────────────┘               │ ReAct Agent  │                  └────────────────┘
                               ├───────────────┤
                               │ Proposer 生成  │ ──► [极速 Reviewer 核查] ──► 幻觉拦截
                               ├───────────────┤
                               │ 语义缓存检查   │ ◄── Redis (Hash 向量匹配)
                               ├───────────────┤
                               │ /api/metrics  │ ◄── Prometheus 抓取
                               └───────┬───────┘
                                       │ OTel Spans
                               ┌───────▼───────┐
                               │   Langfuse    │ ──► 定时 Job (数据飞轮) ──► Pytest 回归
                               └───────────────┘
```

## 技术栈

| 层 | 技术 |
|----|------|
| Agent 编排 | LangGraph · LangChain · `create_react_agent` |
| 后端 | FastAPI · Uvicorn · httpx（连接池复用） |
| 前端 | Next.js 16 · React 19 · TypeScript · Tailwind CSS 4 |
| 检索 | Tavily API · RAG（混合检索）· 知识图谱（BFS 多跳） |
| 记忆 / 缓存 | Redis (UserStore + SessionStore + SemanticCache) |
| 可观测性 | OpenTelemetry · Langfuse · Prometheus · Grafana |
| 配置 | pydantic-settings · python-dotenv |
| 测试 / 评测 | pytest · pytest-asyncio · 自研评测框架 `evals` |
| 交付 | Docker Compose · Kubernetes (HPA) · GitHub Actions |

## Kubernetes 生产级部署

### 架构特性

- **无状态化 Agent**：会话数据完全外置至 Redis，HPA 支持 2~10 副本自动扩缩。
- **RollingUpdate 零中断**：`maxUnavailable: 0` 保证平滑发布。
- **Ingress 路由分流**：`/api/*` 给后端、`/*` 给前端，并为 SSE 关闭 `proxy-buffering`。
- **动态环境覆盖**：支持前端在运行时覆盖默认的环境变量与模型配置。

### 一键部署

```powershell
.\deploy.ps1
```

## 生产数据飞轮（Data Flywheel）

借鉴《深入理解 AI Agent》的持续进化理念，项目打通了从生产监控到自动化回归的闭环：

- **Trace 自动打标**：`server.py` 监听 SSE 流，遇到 `revise`、`fallback` 或 `error` 时，自动向 OTel Span 注入 `langfuse.tags`。
- **轨迹拉取与脱敏**：`scripts/reflow_evals.py` 利用 K8s Job 定时调用 Langfuse API，拉取含有 Bad Case 标签的 Trace，经过内置正则脱敏后写入共享 PVC。
- **回归门禁**：CI/CD 流水线通过 `regression-job` 挂载该 PVC，使用 pytest 执行 `tests/test_reflow_eval.py`，确保历史踩过的坑不再复现。

## API 端点

| 端点 | 方法 | 描述 |
|------|------|------|
| `/api/chat/stream` | POST | SSE 流式聊天（支持透传模型配置，内置语义缓存检查） |
| `/api/session/{id}` | GET | 获取会话历史 |
| `/api/memory/{user_id}` | GET | 查看用户长期事实与情景记忆 |
| `/api/config/defaults` | GET | 获取后端默认生效的模型与配置 |
| `/api/metrics` | GET | Prometheus 文本格式指标 |

### SSE 事件类型

```text
event: progress  → {"node":"search|generate|reviewer|cache","message":"..."}
event: token     → {"text":"逐token文本"}
event: revise    → {"text":"Reviewer 纠错后的完整文本"}
event: sources   → {"sources":[{url,title,snippet}]}
event: done      → {confidence,latency_ms,tokens_used,is_fallback}
event: error     → {message,code}
```

## Agent 工具集

| 工具 | 类别 | 职责 |
|------|------|------|
| `search` | 实时检索 | Tavily 联网搜索，支持长文本落盘 |
| `read_tool_output` | 文件操作 | 读取落盘的超长搜索结果完整内容 |
| `knowledge_search` | 知识库 | 本地 RAG 检索（混合检索 + 时效过滤） |
| `graph_search` | 知识图谱 | 结构化索引检索（关联实体 + 多跳推理路径） |
| `calculator` | 计算 | 安全算术求值（AST 白名单） |
| `current_time` | 时间 | 获取当前 UTC 时间 |

## 可观测性与容错

| 能力 | 说明 |
|------|------|
| 全链路 Trace | 覆盖 ReAct、工具、Reviewer 及 LLM 调用 |
| 成本归因 | 在 Langfuse 按 `user_id` / `session_id` 聚合 Token 消耗 |
| 自动打标 | 发现幻觉、降级或错误时，向 Langfuse 注入 `reviewer_revise` / `fallback` / `error` 标签 |
| ReAct 死循环保护 | `recursion_limit=40` + 状态栏预算警告双重拦截 |
| 优雅降级 | 搜索 API 超时 -> 指数退避；LLM 不可用 -> 切换纯知识回答 |

## 项目结构

```text
qa-sysyywe/
├── agent/            # ReAct 决策、Proposer-Reviewer 审核、SSE 桥接
├── tools/            # 工具注册中心 (search / knowledge_search / read_tool_output)
├── rag/              # RAG 知识库 + 结构化索引
├── memory/           # 记忆系统（支持 File / Redis 双后端）
├── utils/            # 护栏 / 指标 / 压缩 / 语义缓存 / HTTP 连接池
├── k8s/              # K8s 部署清单
│   └── evals/        #   数据飞轮 Job 与 PVC 定义
├── scripts/          # reflow_evals.py (轨迹回流脱敏脚本)
├── tests/            # 220+ 条分层测试 + 生产回归测试
├── frontend/         # Next.js 16 + React 19 (含动态模型设置面板)
├── server.py         # FastAPI 后端入口 (含语义缓存与自动打标)
├── deploy.ps1        # Kubernetes 一键部署脚本
└── docker-compose.yml  # 本地一键编排（4 服务）
```

## License

MIT

---
