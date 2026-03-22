"""
Agent 批量操作服务

支持批量导入导出、批量轮换、批量更新
"""

import csv
import io
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from sqlalchemy import desc
from models import db, Agent, AgentStatus

logger = logging.getLogger(__name__)


class AgentBatchOperationError(Exception):
    """批量操作错误"""
    pass


class AgentBatchOperations:
    """Agent 批量操作"""

    # CSV 导出字段
    EXPORT_FIELDS = [
        'id',
        'name',
        'display_name',
        'description',
        'status',
        'capability_tags',
        'llm_provider',
        'llm_model',
        'temperature',
        'reasoning_mode',
        'created_at',
        'updated_at',
    ]

    def export_agents_to_csv(
        self,
        workspace_id: int,
        agent_ids: Optional[List[int]] = None
    ) -> str:
        """
        导出 Agent 到 CSV

        Args:
            workspace_id: 工作区ID
            agent_ids: 指定导出的 Agent ID 列表，None 则导出全部

        Returns:
            CSV 内容字符串
        """
        query = Agent.query.filter_by(workspace_id=workspace_id)

        if agent_ids:
            query = query.filter(Agent.id.in_(agent_ids))

        agents = query.all()

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=self.EXPORT_FIELDS)
        writer.writeheader()

        for agent in agents:
            row = {}
            for field in self.EXPORT_FIELDS:
                value = getattr(agent, field, None)
                if field == 'capability_tags' and value:
                    row[field] = json.dumps(value)
                elif field in ('created_at', 'updated_at') and value:
                    row[field] = value.isoformat()
                else:
                    row[field] = value
            writer.writerow(row)

        return output.getvalue()

    def export_agents_to_json(
        self,
        workspace_id: int,
        agent_ids: Optional[List[int]] = None,
        include_secrets: bool = False
    ) -> Dict:
        """
        导出 Agent 到 JSON

        Args:
            workspace_id: 工作区ID
            agent_ids: 指定导出的 Agent ID 列表
            include_secrets: 是否包含 Secret（注意：不会包含明文）

        Returns:
            导出数据字典
        """
        query = Agent.query.filter_by(workspace_id=workspace_id)

        if agent_ids:
            query = query.filter(Agent.id.in_(agent_ids))

        agents = query.all()

        export_data = {
            'export_version': '1.0',
            'exported_at': datetime.utcnow().isoformat(),
            'workspace_id': workspace_id,
            'agent_count': len(agents),
            'agents': [],
        }

        for agent in agents:
            agent_data = {
                'id': agent.id,
                'name': agent.name,
                'display_name': agent.display_name,
                'description': agent.description,
                'avatar_url': agent.avatar_url,
                'status': agent.status.value if agent.status else None,
                'capability_tags': agent.capability_tags,
                'system_prompt': agent.system_prompt,
                'soul_markdown': agent.soul_markdown,
                'response_style': agent.response_style,
                'tool_policy': agent.tool_policy,
                'memory_policy': agent.memory_policy,
                'handoff_policy': agent.handoff_policy,
                'llm_provider': agent.llm_provider,
                'llm_model': agent.llm_model,
                'temperature': str(agent.temperature) if agent.temperature else None,
                'reasoning_mode': agent.reasoning_mode,
            }
            export_data['agents'].append(agent_data)

        return export_data

    def import_agents_from_json(
        self,
        workspace_id: int,
        user_id: int,
        data: Dict,
        import_mode: str = 'create'  # 'create' 或 'update'
    ) -> Dict:
        """
        从 JSON 导入 Agent

        Args:
            workspace_id: 工作区ID
            user_id: 操作用户ID
            data: 导入数据
            import_mode: 导入模式

        Returns:
            导入结果统计
        """
        results = {
            'total': 0,
            'created': 0,
            'updated': 0,
            'skipped': 0,
            'failed': 0,
            'errors': [],
        }

        agents_data = data.get('agents', [])
        results['total'] = len(agents_data)

        for agent_data in agents_data:
            try:
                existing = Agent.query.filter_by(
                    workspace_id=workspace_id,
                    name=agent_data['name']
                ).first()

                if existing and import_mode == 'create':
                    results['skipped'] += 1
                    continue

                if existing and import_mode == 'update':
                    # 更新现有 Agent
                    existing.display_name = agent_data.get('display_name', existing.display_name)
                    existing.description = agent_data.get('description', existing.description)
                    existing.capability_tags = agent_data.get('capability_tags', existing.capability_tags)
                    existing.system_prompt = agent_data.get('system_prompt', existing.system_prompt)
                    existing.llm_provider = agent_data.get('llm_provider', existing.llm_provider)
                    existing.llm_model = agent_data.get('llm_model', existing.llm_model)
                    existing.updated_by_user_id = user_id
                    results['updated'] += 1
                else:
                    # 创建新 Agent
                    agent = Agent(
                        workspace_id=workspace_id,
                        creator_user_id=user_id,
                        name=agent_data['name'],
                        display_name=agent_data.get('display_name'),
                        description=agent_data.get('description'),
                        capability_tags=agent_data.get('capability_tags', []),
                        system_prompt=agent_data.get('system_prompt'),
                        llm_provider=agent_data.get('llm_provider'),
                        llm_model=agent_data.get('llm_model'),
                        reasoning_mode=agent_data.get('reasoning_mode', 'balanced'),
                        status=AgentStatus.ACTIVE,
                    )
                    db.session.add(agent)
                    results['created'] += 1

            except Exception as e:
                logger.error(f"Failed to import agent {agent_data.get('name')}: {e}")
                results['failed'] += 1
                results['errors'].append({
                    'name': agent_data.get('name'),
                    'error': str(e),
                })

        db.session.commit()
        return results

    def batch_rotate_secrets(
        self,
        workspace_id: int,
        agent_ids: List[int],
        user_id: int
    ) -> Dict:
        """
        批量轮换 Agent Secret

        Args:
            workspace_id: 工作区ID
            agent_ids: Agent ID 列表
            user_id: 操作用户ID

        Returns:
            轮换结果
        """
        from models import AgentSecret

        results = {
            'total_agents': len(agent_ids),
            'total_secrets': 0,
            'rotated': 0,
            'failed': 0,
            'errors': [],
        }

        for agent_id in agent_ids:
            try:
                secrets = AgentSecret.query.filter_by(
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    is_active=True
                ).all()

                for secret in secrets:
                    try:
                        secret.rotate_encryption(user_id)
                        results['rotated'] += 1
                    except Exception as e:
                        logger.error(f"Failed to rotate secret {secret.id}: {e}")
                        results['failed'] += 1

                results['total_secrets'] += len(secrets)

            except Exception as e:
                logger.error(f"Failed to process agent {agent_id}: {e}")
                results['errors'].append({
                    'agent_id': agent_id,
                    'error': str(e),
                })

        db.session.commit()
        return results

    def batch_update_agent_status(
        self,
        workspace_id: int,
        agent_ids: List[int],
        new_status: AgentStatus,
        user_id: int
    ) -> Dict:
        """
        批量更新 Agent 状态

        Args:
            workspace_id: 工作区ID
            agent_ids: Agent ID 列表
            new_status: 新状态
            user_id: 操作用户ID

        Returns:
            更新结果
        """
        results = {
            'total': len(agent_ids),
            'updated': 0,
            'failed': 0,
            'errors': [],
        }

        for agent_id in agent_ids:
            try:
                agent = Agent.query.filter_by(
                    id=agent_id,
                    workspace_id=workspace_id
                ).first()

                if agent:
                    agent.status = new_status
                    agent.updated_by_user_id = user_id
                    results['updated'] += 1
                else:
                    results['failed'] += 1
                    results['errors'].append({
                        'agent_id': agent_id,
                        'error': 'Agent not found',
                    })

            except Exception as e:
                logger.error(f"Failed to update agent {agent_id}: {e}")
                results['failed'] += 1
                results['errors'].append({
                    'agent_id': agent_id,
                    'error': str(e),
                })

        db.session.commit()
        return results

    def batch_delete_agents(
        self,
        workspace_id: int,
        agent_ids: List[int],
        user_id: int,
        force: bool = False
    ) -> Dict:
        """
        批量删除 Agent

        Args:
            workspace_id: 工作区ID
            agent_ids: Agent ID 列表
            user_id: 操作用户ID
            force: 是否强制删除（有关联数据也删除）

        Returns:
            删除结果
        """
        results = {
            'total': len(agent_ids),
            'deleted': 0,
            'failed': 0,
            'errors': [],
        }

        for agent_id in agent_ids:
            try:
                agent = Agent.query.filter_by(
                    id=agent_id,
                    workspace_id=workspace_id
                ).first()

                if not agent:
                    results['failed'] += 1
                    results['errors'].append({
                        'agent_id': agent_id,
                        'error': 'Agent not found',
                    })
                    continue

                if not force:
                    # 检查关联数据
                    if agent.secrets and agent.secrets.count() > 0:
                        results['failed'] += 1
                        results['errors'].append({
                            'agent_id': agent_id,
                            'error': 'Agent has secrets, use force=true to delete',
                        })
                        continue

                db.session.delete(agent)
                results['deleted'] += 1

            except Exception as e:
                logger.error(f"Failed to delete agent {agent_id}: {e}")
                results['failed'] += 1
                results['errors'].append({
                    'agent_id': agent_id,
                    'error': str(e),
                })

        db.session.commit()
        return results


# 全局实例
_batch_ops: Optional[AgentBatchOperations] = None


def get_batch_operations() -> AgentBatchOperations:
    """获取批量操作实例"""
    global _batch_ops
    if _batch_ops is None:
        _batch_ops = AgentBatchOperations()
    return _batch_ops
