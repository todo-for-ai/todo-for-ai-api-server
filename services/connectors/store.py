"""连接器配置存取：workspace + provider 一条配置，secret 加密落库。"""

import structlog

from models import ExternalConnectorConfig, db
from services.github_app import encrypt_str

logger = structlog.get_logger()


def get_connector(workspace_id: int, provider: str):
    return ExternalConnectorConfig.query.filter_by(
        workspace_id=workspace_id, provider=provider,
    ).first()


def list_connectors(workspace_id: int):
    return ExternalConnectorConfig.query.filter_by(workspace_id=workspace_id).all()


def upsert_connector(workspace_id: int, provider: str, data: dict) -> ExternalConnectorConfig:
    config = get_connector(workspace_id, provider)
    if not config:
        config = ExternalConnectorConfig(workspace_id=workspace_id, provider=provider)
        db.session.add(config)
    if 'enabled' in data:
        config.enabled = bool(data['enabled'])
    if 'default_project_id' in data and data['default_project_id']:
        config.default_project_id = int(data['default_project_id'])
    if data.get('secret'):
        config.secret_encrypted = encrypt_str(str(data['secret']))
    db.session.commit()
    logger.info("connector.config_upserted", workspace_id=workspace_id, provider=provider)
    return config
