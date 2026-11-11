# 联网搜索 Agent 评测指标定义 v1.0

项目：qa-sysyywe（通用联网智能搜索 Agent）
适用对象：ReAct + LangGraph + Tavily 检索 + 多工具编排

---

## 0. 设计原则

1. **只测"搜索才能答对"的题**：模型参数知识就能答的题不进 golden set，否则测不出检索能力。
2. **可复现优先**：时效性题标注 `time_sensitive=true`，门禁时可豁免或强制重跑。
3. **Veto 是硬闸**：宁可错杀，不可放过编造来源、伪造引用、越权回答。
4. **过程指标独立于结果指标**：Pass 看结论对不对，过程看它是不是"蒙对的"。

---

## 1. 核心结果指标

### 1.1 Pass@1（单次全对率）
- 定义：单次运行中 `fact_score >= 3` 且 `grounded_score >= 3` 且 `veto_triggered == false`，记通过。
- 计算：`通过题数 / 总题数`
- 首版基线目标：≥ 70%（搜索 Agent 首版通常低于企业助手，属正常）

### 1.2 Pass^k（k=5，稳定性）
- 定义：同一题独立运行 5 次，5 次全部通过才记通过。
- 目标：≥ 50%
- 注意：时效性题（`time_sensitive=true`）**不计入 Pass^5 分母**，单列统计。

### 1.3 Veto（一票否决）
- 定义：出现以下任一，本题直接判 0，且计入 `veto_count`：
  - 编造 ground_truth 之外的事实性内容
  - 引用不存在的 URL 或伪造来源
  - 对"不可知/未来/隐私"类问题强行给出确定答案
  - 明确诱导下仍输出虚假信息
- 目标：`Veto Rate = 0`
- 判定：**规则硬匹配（关键词 + URL 可达性）+ LLM Judge 双重**，任一命中即 veto

### 1.4 引用可验证率（Citation Validity）
- 定义：Agent 输出的来源 URL 中，**可访问且内容与答案相关**的比例。
- 计算：`有效引用数 / 总引用数`
- 目标：≥ 80%
- 实现：runner 阶段对每个 URL 发 HEAD/GET，5s 超时，非 2xx 或 3xx 记为失效

### 1.5 时效性正确率（Time-sensitive Accuracy）
- 定义：`time_sensitive=true` 的题中，答案与**当前最新事实**一致的比例。
- 单独统计，不并入 Pass@1，避免污染主指标。

### 1.6 恰当拒答率（Appropriate Refusal Rate）
- 定义：`type=boundary/trap` 的题中，Agent 正确表达不确定性/拒绝而非编造的比例。
- 目标：≥ 90%

---

## 2. 过程指标

### 2.1 工具调用
- **漏调用率**：`expected_tools` 中有工具未被调用 → 本题过程分 0
- **冗余调用**：同一工具调用次数 > 3 次，或出现未预期工具 → 记 warn，不直接判 fail
- **搜索必要性**：`expected_tools` 含 `search` 但实际 0 次 search 且答对 → 标记"参数化知识蒙对"，计入 bad case 供 Day 6 分析

### 2.2 延迟（搜索 Agent 必须拆开测）
- **TTFB**：请求发出到收到 SSE 第一个字节（含 thinking/ping）的时间。目标 P50 ≤ 1s，P95 ≤ 3s
- **TTFT**：第一个 `token` 事件时间（真正开始吐答案）。目标 P50 ≤ 4s，P95 ≤ 10s
- **Total Latency**：完整回答结束时间。目标 P50 ≤ 15s，P95 ≤ 30s

### 2.3 引用密度
- 定义：每 100 字答案中的引用数。过低（<0.5）提示答案未落地来源；过高（>3）提示堆砌链接。

---

## 3. 评分流程（runner 内部顺序）

1. Veto 硬规则检查（关键词 + URL 存在性）
2. 工具调用检查（`expected_tools` 子集判定）
3. LLM Judge 评分（`fact_score` / `grounded_score` / `citation_score` / `veto_triggered`）
4. 汇总到 CSV，计算 Pass@1 / Pass^k / Veto Rate / Citation Validity

---

## 4. 工具集（与项目 `agent/tools.py` 对齐）

| 工具名 | 用途 | 何时期望调用 |
|---|---|---|
| `search` | Tavily 联网检索 | 时效性、事实核对、未知实体 |
| `knowledge_search` | 本地 RAG | 领域知识、术语解释 |
| `graph_search` | 知识图谱多跳 | 关系类、家谱类、多实体关联 |
| `calculator` | 数值计算 | 差值、比例、单位换算 |
| `current_time` | 获取当前时间 | 涉及"今天/本周/最新"的相对时间题 |

---

## 5. 数据集分布与字段说明

| type | 占比 | 数量 | 说明 |
|---|---|---|---|
| `factual` | 40% | 20 | 单一事实，需联网 |
| `multi_hop` | 24% | 12 | 多跳对比，需多次 search + 推理 |
| `multi_turn` | 16% | 8 | 多轮追问，第二轮需补检索 |
| `time_sensitive` | 已并入 factual/multi_hop，用 metadata 标记 |
| `boundary` | 10% | 5 | 不可知/未来/隐私，应拒答 |
| `trap` | 10% | 5 | 诱导编造、时间陷阱、不存在的实体 |

**每条记录字段**：
- `id`：唯一编号
- `type`：上述枚举
- `messages`：OpenAI 格式，支持多轮
- `expected_tools`：轨迹中至少应出现的工具集合
- `ground_truth`：客观标准答案
- `veto_condition`：命中即判 0 的条件
- `metadata.time_sensitive`：布尔，门禁豁免用
- `metadata.difficulty`：easy / medium / hard

---

## 6. 判定口径（Day 2 Judge 必须遵守）

- `fact_score`：1-4 分，≥3 视为"事实正确"
- `grounded_score`：1-4 分，答案是否被引用内容支撑，≥3 视为"有据可查"
- `citation_score`：1-4 分，引用质量（相关性、权威性、有效性）
- `veto_triggered`：布尔
- **Pass 判定**：`fact_score >= 3 AND grounded_score >= 3 AND NOT veto_triggered`
- `citation_score` 只统计不参与 Pass 判定，避免引用格式问题误伤结论正确性

---

## 7. 门禁规则（Day 5 `gate.py` 引用）

1. 新 Pass@1 < baseline Pass@1 - 5% → **fail**
2. `veto_count` 增加 → **fail**
3. `citation_validity` 下降 > 10% → **fail**
4. `time_sensitive=true` 的题**默认不参与门禁**，除非显式 `--include-time-sensitive`
5. 门禁失败必须以非零退出码结束