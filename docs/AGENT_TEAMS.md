# Agent Teams 功能文档

## 概述

Agent Teams 功能允许用户创建和管理 Agent 团队，实现多 Agent 协作处理复杂任务。系统内置了 8 种预定义角色模板，用户可以直接使用或基于其创建自定义 Agent。

## 核心功能

### 1. 预定义角色模板 (Agent Role Templates)

#### 内置模板列表

| 角色 | 名称 | 描述 |
|------|------|------|
| developer | Developer | 专注于代码实现和功能开发 |
| reviewer | Code Reviewer | 专注于代码审查和质量保障 |
| architect | System Architect | 专注于系统架构和技术决策 |
| qa | QA Engineer | 专注于测试策略和质量保证 |
| pm | Product Manager | 专注于产品规划和需求分析 |
| ux-designer | UX Designer | 专注于用户体验和交互设计 |
| analyst | Data Analyst | 专注于数据分析和洞察发现 |
| writer | Technical Writer | 专注于技术文档和内容创作 |

#### API 端点

```
GET    /workspaces/{id}/agent-role-templates              # 列出模板
GET    /workspaces/{id}/agent-role-templates/{id}         # 获取模板详情
POST   /workspaces/{id}/agent-role-templates              # 创建自定义模板
PUT    /workspaces/{id}/agent-role-templates/{id}         # 更新模板
DELETE /workspaces/{id}/agent-role-templates/{id}         # 删除模板
POST   /workspaces/{id}/agent-role-templates/{id}/instantiate  # 从模板创建 Agent
GET    /workspaces/{id}/agent-role-templates/categories   # 获取分类列表
```

### 2. Agent 团队 (Agent Teams)

#### 功能特性

- **团队创建和管理**: 支持创建多个团队，设置团队名称、描述和默认编排策略
- **成员管理**: 支持添加/移除 Agent，设置角色（leader/member/specialist/observer），调整执行顺序
- **项目关联**: 团队可以与多个项目关联
- **编排策略配置**: sequential, parallel, map_reduce, debate

#### API 端点

```
# Team CRUD
GET    /workspaces/{id}/agent-teams                       # 列出团队
POST   /workspaces/{id}/agent-teams                       # 创建团队
GET    /workspaces/{id}/agent-teams/{id}                  # 获取团队详情
PUT    /workspaces/{id}/agent-teams/{id}                  # 更新团队
DELETE /workspaces/{id}/agent-teams/{id}                  # 删除团队

# 团队成员管理
GET    /workspaces/{id}/agent-teams/{id}/members          # 列出成员
POST   /workspaces/{id}/agent-teams/{id}/members          # 添加成员
PUT    /workspaces/{id}/agent-teams/{id}/members/{id}     # 更新成员
DELETE /workspaces/{id}/agent-teams/{id}/members/{id}     # 移除成员
POST   /workspaces/{id}/agent-teams/{id}/members/reorder  # 调整顺序

# 团队项目关联
GET    /workspaces/{id}/agent-teams/{id}/projects         # 列出关联项目
POST   /workspaces/{id}/agent-teams/{id}/projects         # 关联项目
DELETE /workspaces/{id}/agent-teams/{id}/projects/{id}    # 解除关联
```

### 3. 团队任务编排 (Task Orchestration)

#### 编排策略

| 策略 | 描述 |
|------|------|
| sequential | 顺序执行，每个 Agent 完成后再交给下一个 |
| parallel | 并行执行，所有 Agent 同时处理 |
| map_reduce | MapReduce 模式，先分发处理再聚合结果 |
| debate | 辩论模式，多轮讨论达成共识 |

#### API 端点

```
# 用户 API
POST   /workspaces/{id}/tasks/{id}/orchestrate            # 启动编排
GET    /workspaces/{id}/orchestrations/{id}               # 获取编排详情
POST   /workspaces/{id}/orchestrations/{id}/start         # 开始执行
POST   /workspaces/{id}/orchestrations/{id}/cancel        # 取消编排
POST   /workspaces/{id}/orchestrations/{id}/aggregate     # 手动聚合结果

# Agent Runtime API
GET    /agent/team/subtasks                               # Agent 拉取子任务
POST   /agent/team/subtasks/{id}/accept                   # 接受子任务
POST   /agent/team/subtasks/{id}/complete                 # 完成子任务
POST   /agent/team/subtasks/{id}/fail                     # 报告失败
```

## 数据模型

### 核心表结构

```
agent_role_templates    # 角色模板表
  - id, workspace_id, name, display_name, description
  - category, capability_tags, system_prompt, soul_markdown
  - response_style, tool_policy, memory_policy, handoff_policy
  - is_builtin, status, usage_count

agent_teams             # 团队表
  - id, workspace_id, name, description
  - config, default_strategy, status
  - member_count, task_count

agent_team_members      # 团队成员关系表
  - id, team_id, agent_id, role, order_index
  - responsibility, config, notifications_enabled

agent_team_projects     # 团队项目关联表
  - id, team_id, project_id, workspace_id, config, role

team_task_orchestrations  # 任务编排表
  - id, team_id, task_id, workspace_id
  - strategy, participating_agent_ids, status
  - current_stage, total_stages, output_aggregator_agent_id
  - result_payload, config, started_at, completed_at

team_subtasks           # 子任务表
  - id, orchestration_id, assigned_agent_id
  - title, description, stage_index, order_index
  - depends_on_subtask_ids, status
  - input_payload, output_payload
  - started_at, completed_at, attempt_count, last_error
```

## 使用流程

### 1. 从模板创建 Agent

```bash
# 1. 查看可用模板
GET /workspaces/123/agent-role-templates

# 2. 基于模板创建 Agent
POST /workspaces/123/agent-role-templates/1/instantiate
{
  "name": "my-developer",
  "display_name": "My Developer"
}
```

### 2. 创建团队并添加成员

```bash
# 1. 创建团队
POST /workspaces/123/agent-teams
{
  "name": "Backend Team",
  "description": "负责后端开发的团队",
  "default_strategy": "sequential"
}

# 2. 添加成员
POST /workspaces/123/agent-teams/1/members
{
  "agent_id": 1,
  "role": "leader",
  "order_index": 0
}
```

### 3. 启动编排任务

```bash
# 启动编排
POST /workspaces/123/tasks/456/orchestrate
{
  "team_id": 1,
  "strategy": "sequential",
  "participating_agent_ids": [1, 2, 3],
  "output_aggregator": 1
}

# 开始执行
POST /workspaces/123/orchestrations/789/start
```

## 内置模板加载

```bash
# 进入 API server 目录
cd todo-for-ai-api-server

# 加载内置模板
python scripts/load_builtin_templates.py

# 重新加载（清空后重新加载）
python scripts/load_builtin_templates.py --clear

# 查看已加载的模板
python scripts/load_builtin_templates.py --list
```

## 数据库迁移

```bash
# 运行迁移
python -m flask db migrate -m "Add agent teams"

# 或者直接应用（如果是新部署）
python -m flask db upgrade
```

## 权限说明

- 内置模板：所有用户可见，不可修改
- 自定义模板：仅创建者 workspace 可见，可修改
- Team 管理：需要 workspace 访问权限
- 编排执行：遵循 task 和 agent 的权限规则

## 扩展开发

### 添加新的角色模板

1. 在 `data/agent_role_templates/` 创建新的 JSON 文件
2. 运行加载脚本：`python scripts/load_builtin_templates.py`

### 添加新的编排策略

1. 在 `models/team_task_orchestration.py` 的 `OrchestrationStrategy` 添加新策略
2. 在 `api/agent_team_orchestration.py` 的 `_auto_create_subtasks` 实现策略逻辑
3. 创建数据库迁移
