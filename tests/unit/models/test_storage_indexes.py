"""存储层热路径索引回归：模型 create_all 出来的库必须带这些复合索引，
与迁移 20260910_000025_add_storage_hotpath_indexes 保持一致。

这三个索引对应活库 EXPLAIN 审计出的三个缺口：
- 工作区/Agent 维度「正在干活」水位（编排容量门禁，每次 pull 都查询）；
- Agent 最新心跳（原先全表扫描 + filesort）。
"""

from sqlalchemy import inspect

from models import db


def _index_map(table):
    inspector = inspect(db.engine)
    return {
        ix['name']: list(ix['column_names'])
        for ix in inspector.get_indexes(table)
    }


def test_lease_capacity_indexes_exist(app):
    indexes = _index_map('agent_task_leases')
    assert indexes['idx_leases_workspace_active_exp'] == \
        ['workspace_id', 'active', 'expires_at', 'agent_id']
    assert indexes['idx_leases_agent_active_exp'] == \
        ['agent_id', 'active', 'expires_at']


def test_heartbeat_latest_index_exists(app):
    indexes = _index_map('agent_heartbeats')
    assert indexes['idx_agent_heartbeats_agent_created'] == \
        ['agent_id', 'created_at']


def test_lease_unique_constraint_still_declared(app):
    inspector = inspect(db.engine)
    uniques = {
        uq['name'] for uq in inspector.get_unique_constraints('agent_task_leases')
    }
    assert 'uq_agent_task_leases_task_active' in uniques
