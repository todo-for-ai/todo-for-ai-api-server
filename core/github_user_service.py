"""
GitHub 用户服务模块 - 负责用户信息获取和创建/更新
"""

import requests
from datetime import datetime
from flask import current_app
from models import User


class GitHubUserService:
    """GitHub 用户服务类"""
    
    def get_user_info(self, access_token):
        """获取 GitHub 用户信息"""
        try:
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Accept': 'application/vnd.github.v3+json'
            }
            
            user_response = requests.get('https://api.github.com/user', headers=headers, timeout=180)
            user_response.raise_for_status()
            user_data = user_response.json()
            
            if not user_data.get('email'):
                email_response = requests.get('https://api.github.com/user/emails', headers=headers, timeout=180)
                if email_response.status_code == 200:
                    emails = email_response.json()
                    primary_email = next((email['email'] for email in emails if email['primary']), None)
                    if primary_email:
                        user_data['email'] = primary_email
            
            return user_data
        except Exception as e:
            current_app.logger.error(f"获取GitHub用户信息失败: {str(e)}")
            return None
    
    def create_or_update_user(self, github_user_info, defaults_service=None):
        """创建或更新用户"""
        try:
            github_id = str(github_user_info['id'])
            username = github_user_info['login']
            email = github_user_info.get('email', f"{username}@github.local")
            avatar_url = github_user_info.get('avatar_url')
            name = github_user_info.get('name', username)

            user = User.query.filter_by(github_id=github_id).first()
            is_new_user = user is None

            if user:
                user.username = username
                user.email = email
                user.avatar_url = avatar_url
                user.name = name
                # 同时更新两个登录时间字段以确保兼容性
                user.last_login = datetime.utcnow()
                user.last_login_at = datetime.utcnow()
            else:
                user = User(
                    github_id=github_id,
                    username=username,
                    email=email,
                    avatar_url=avatar_url,
                    name=name,
                    # 同时设置两个登录时间字段以确保兼容性
                    last_login=datetime.utcnow(),
                    last_login_at=datetime.utcnow()
                )
                from models import db
                db.session.add(user)

            from models import db
            db.session.commit()

            # 为新用户自动创建默认数据
            if is_new_user and defaults_service:
                from flask import request as flask_request
                defaults_service.create_all_defaults(user, flask_request)

            return user
        except Exception as e:
            current_app.logger.error(f"创建或更新用户失败: {str(e)}")
            from models import db
            db.session.rollback()
            return None


# 创建全局实例
github_user_service = GitHubUserService()
