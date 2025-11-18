"""
Google OAuth 配置和服务
"""

import os


class GoogleConfig:
    def __init__(self):
        self.client_id = os.environ.get('GOOGLE_CLIENT_ID')
        self.client_secret = os.environ.get('GOOGLE_CLIENT_SECRET')


class GoogleService:
    def __init__(self, app=None):
        self.app = app
        self.config = None

        if app:
            self.init_app(app)

    def init_app(self, app):
        self.app = app
        self.config = GoogleConfig()

        # 检查配置
        if not self.config.client_id or not self.config.client_secret:
            raise ValueError("Google OAuth 配置缺失")


google_service = GoogleService()
