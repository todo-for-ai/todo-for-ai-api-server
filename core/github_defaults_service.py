"""
GitHub 默认数据服务模块 - 负责为新用户创建默认数据
"""

from flask import current_app
from models import db


class GitHubDefaultsService:
    """GitHub 默认数据服务类"""
    
    def create_all_defaults(self, user, request=None):
        """为新用户创建所有默认数据"""
        self.create_default_api_token(user)
        user_language = self.create_default_user_settings(user, request)
        self.create_default_global_rule(user)
        self.create_default_custom_prompts(user, user_language)
    
    def create_default_api_token(self, user):
        """为新用户创建默认的API Token"""
        try:
            from models import ApiToken

            # 检查用户是否已有API Token
            existing_token = ApiToken.query.filter_by(user_id=user.id).first()
            if existing_token:
                return  # 已有Token，不需要创建

            # 生成永不过期的默认Token
            api_token, token = ApiToken.generate_token(
                name="默认Token",
                description="系统自动生成的默认API Token，用于MCP客户端认证",
                expires_days=None  # 永不过期
            )

            # 设置用户ID
            api_token.user_id = user.id

            db.session.add(api_token)
            db.session.commit()

            current_app.logger.info(f"为用户 {user.email} 创建了默认API Token")

        except Exception as e:
            current_app.logger.error(f"创建默认API Token失败: {str(e)}")
            db.session.rollback()

    def create_default_user_settings(self, user, request=None):
        """为新用户创建默认的用户设置"""
        try:
            from models import UserSettings

            # 防止递归调用
            if hasattr(user, '_creating_settings'):
                return 'zh-CN'  # 返回默认语言
            user._creating_settings = True

            # 检查用户是否已有设置
            existing_settings = UserSettings.query.filter_by(user_id=user.id).first()
            if existing_settings:
                return existing_settings.language  # 返回已有设置的语言

            # 检测用户语言偏好
            default_language = self._detect_user_language(user, request)

            # 创建默认设置
            settings = UserSettings(
                user_id=user.id,
                language=default_language,
                settings_data={}
            )

            db.session.add(settings)
            db.session.commit()

            current_app.logger.info(f"为用户 {user.email} 创建了默认用户设置，语言: {default_language}")
            return default_language

        except Exception as e:
            current_app.logger.error(f"创建默认用户设置失败: {str(e)}")
            db.session.rollback()
            return 'zh-CN'  # 返回默认语言
        finally:
            # 清理递归标记
            if hasattr(user, '_creating_settings'):
                delattr(user, '_creating_settings')

    def _detect_user_language(self, user, request=None):
        """检测用户的语言偏好"""
        # 1. 优先使用用户的locale字段
        if user.locale and user.locale.startswith('zh'):
            return 'zh-CN'

        # 2. 如果有请求对象，检查Accept-Language头
        if request:
            accept_language = request.headers.get('Accept-Language', '')
            if 'zh' in accept_language.lower():
                return 'zh-CN'

        # 3. 默认返回英语
        return 'en'

    def create_default_global_rule(self, user):
        """为新用户创建默认的全局规则"""
        try:
            from models import ContextRule

            # 检查用户是否已有全局规则
            existing_rule = ContextRule.query.filter_by(
                user_id=user.id,
                project_id=None  # 全局规则
            ).first()
            if existing_rule:
                return  # 已有全局规则，不需要创建

            # 创建默认全局规则
            rule_content = """用户界面交互UI设计的四个基本原则：亲密性、对齐、重复和对比。

1. **亲密性**
   相关元素放得近，无关元素分开排。
   这样分组更清晰，用户一看就明白。

2. **对齐**
   所有元素要对齐，左中右都要整齐。
   随便乱摆会显乱，对齐才能更专业。

3. **重复**
   同样样式重复用，颜色字体要统一。
   保持风格一致性，用户习惯更容易。

4. **对比**
   重要内容要突出，大小颜色差别大。
   对比越强越显眼，用户一眼就看到。"""

            default_rule = ContextRule(
                user_id=user.id,
                project_id=None,  # 全局规则
                name="用户界面交互UI设计的基本原则",
                description="系统自动创建的默认全局规则，包含UI设计的四个基本原则",
                content=rule_content,
                priority=0,
                is_active=True,
                apply_to_tasks=True,
                apply_to_projects=False,
                is_public=False,
                usage_count=0,
                created_by='system'
            )

            db.session.add(default_rule)
            db.session.commit()

            current_app.logger.info(f"为用户 {user.email} 创建了默认全局规则")

        except Exception as e:
            current_app.logger.error(f"创建默认全局规则失败: {str(e)}")
            db.session.rollback()

    def create_default_custom_prompts(self, user, language='zh-CN'):
        """为新用户创建默认的自定义提示词"""
        try:
            from models import CustomPrompt

            # 检查用户是否已有提示词
            existing_count = CustomPrompt.query.filter(CustomPrompt.user_id == user.id).count()
            if existing_count > 0:
                return  # 已有提示词，不需要创建

            # 初始化默认提示词
            CustomPrompt.initialize_user_defaults(user.id, language)

            current_app.logger.info(f"为用户 {user.email} 创建了默认自定义提示词，语言: {language}")

        except Exception as e:
            current_app.logger.error(f"创建默认自定义提示词失败: {str(e)}")
            db.session.rollback()


# 创建全局实例
github_defaults_service = GitHubDefaultsService()
