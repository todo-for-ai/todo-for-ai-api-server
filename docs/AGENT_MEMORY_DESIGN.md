# Agent 记忆管理设计 —— 自建 vs 引入开源框架的评估与决策

v1，2026-09-13。配套实现：`feat/agent-memory-layer`（可插拔记忆层 Phase 1）。

## 1. 平台已有的记忆基建（盘点结论）

| 层 | 载体 | 状态 |
|---|---|---|
| 循环工作记忆 | GoalLoop 上下文走廊（目标层/压缩层/明细层，ENDURANCE_MODE_DESIGN §9） | ✅ 已落地 |
| 结构化经验 | `AgentExperience`（成功/失败/策略模式，置信度衰减/共享/交叉验证/复用计数） | ✅ 已落地 |
| 知识库 | `KnowledgeEntry` + 提案确认管线（失败归因/PR 评审/人工纠正 → 人确认成规则） | ✅ 已落地 |
| 聚合画像 | `skill_profile` + `AgentSoulVersion`（版本化/审计/GDPR 式删除的记忆治理） | ✅ 已落地 |
| 派单消费 | experience_bonus / skill_profile_bonus / reputation 进派发打分 | ✅ 已落地 |
| **语义检索** | ——（只有 `ilike` 词面匹配和 JSON contains） | ❌ 缺 |
| **自动注入** | ——（`experiences/recommend` 是拉取 API，从不自动进 prompt） | ❌ 缺（本次补上） |
| **语义去重/合并** | ——（dedupe 是字符串键） | ❌ 缺 |
| 跨项目/组织级记忆命名空间 | ——（共享是布尔 `is_shared`） | ❌ 缺 |

结论：**记忆的「写入、治理、消费」三层平台都有且相当完整；缺的是「语义检索 + 自动注入」这最后一公里。**

## 2. 开源框架评估（2026-09 现状）

| 框架 | 许可证 | 核心思路 | 引入代价 | 适配判定 |
|---|---|---|---|---|
| **mem0** | Apache-2.0 | 事实抽取→向量库→语义召回，20+ 向量后端（**含 Redis**） | `pip install mem0ai` + embedding 模型 + 抽取 LLM 调用；向量后端可复用平台现有 Redis | ★ 最适合做**可选语义后端** |
| **Graphiti / Zep** | Graphiti Apache-2.0；但后端 Neo4j 社区版 GPLv3 / FalkorDB SSPL | 双时序知识图谱（实体/关系/时间边） | 必须新增图数据库 + 抽取 LLM；运维面扩大 | 数据模型最好，但当前无图数据库需求，**暂缓** |
| **Letta（MemGPT）** | Apache-2.0 | 完整的有状态 Agent 服务器 + 自编辑记忆块 | 会**架空平台自有 agent-runtime** | ❌ 定位冲突 |
| **cognee** | Apache-2.0 | 图+向量+关系混合语义记忆 | 同样要图库+向量库 | 同 Graphiti，暂缓 |
| **LangMem** | MIT | LangChain/LangGraph 生态的记忆库 | 绑定 LangGraph 栈 | ❌ 技术栈不符 |

关键制约：平台数据栈是 MySQL 8 + Redis 7，无 pgvector/图库；自托管优先、依赖越少越好；
且记忆治理（版本/审计/删除）平台已自建，框架在这方面并无增量。

## 3. 决策：可插拔记忆层（自建为底，mem0 为可选语义后端）

**不整体引入任何框架**（没有一家是零新基础设施的纯嵌入层；Letta 定位冲突），
但把记忆召回抽象成后端无关接口，语义能力以适配器形式增量引入：

```
services/memory/
├── __init__.py        # MemoryHit / get_memory_backend() 工厂 / recall_for_query()
├── builtin.py         # 默认后端：AgentExperience + KnowledgeEntry 词面召回（置信度加权）
└── mem0_backend.py    # 可选后端：mem0ai 适配（Redis 向量后端，复用现有 Redis）
```

- `AGENT_MEMORY_BACKEND=builtin`（默认，零新依赖）| `mem0`（装 mem0ai + 配 embedding 后切换）；
- mem0 不可用（未安装/配置缺失/调用失败）时工厂与调用点**双重自动回退 builtin**；
- 切换后端不改任何业务代码（GoalLoop 派发是唯一调用方，后续可扩到 auto_assign）。

### Phase 1（本次已落地）

1. **自动注入**：`create_round_task` 按步骤标题+内容召回 top-K（`AGENT_MEMORY_TOP_K`，
   默认 3）记忆，以【相关记忆（历史经验/知识库）】块注入轮次任务内容（上限
   `AGENT_MEMORY_SECTION_CHARS` 默认 800 字符）——**首轮也注入**（记忆与循环历史无关）；
   召回失败静默跳过，绝不阻断派发。
2. **成功经验写入**：循环 DONE 时写 `success_pattern` 经验（达成策略/完成总结，
   与失败路径的 failure_pattern 对称）——此前成功经验没有落点。
3. builtin 后端的中文友好检索：CJK 二元切分（无空格分词）+ 多关键词去重加权排序。

### Phase 2（规模化后触发）

- 启用 mem0 后端（Redis vector set 复用现有 Redis；embedding 走平台 OpenAI 兼容端点）；
- 经验/知识写入路径接 mem0 `add()` 做事实抽取与语义合并（治「语义去重缺失」）；
- 派发打分（auto_assign）接入记忆召回做任务级经验匹配。

### Phase 3（多 Agent 协作图谱成熟后）

- 评估 Graphiti 双时序图谱（用 FalkorDB 规避 Neo4j GPLv3）建模「谁在什么时候
  和谁协作产出了什么」，服务多 Agent 协作审计与追溯。

## 4. 风险与兼容

- builtin 词面召回的局限（无语义泛化）是**已知且有意的**——先保证零依赖可用，
  语义召回由 mem0 适配器增量补齐，接口不变。
- 记忆注入增加轮次任务内容的长度，但有 800 字符 section 上限 + 走廊整体 6000
  字符上界双重钳制，prompt 规模仍确定有界。
- mem0 适配器当前不进 requirements.txt（可选依赖），部署按需安装，避免全量用户
  背上向量栈。
