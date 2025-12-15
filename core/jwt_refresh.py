"""
JWT Token自动续期中间件

当Token剩余有效期少于总有效期的1/3时，自动在响应头中返回新的Token
前端应检查X-New-Token响应头，如果存在则更新本地Token
"""

from datetime import datetime, timezone
from flask import request, g
from flask_jwt_extended import get_jwt, create_access_token, get_jwt_identity, verify_jwt_in_request
from functools import wraps


def should_refresh_token(jwt_data):
    """
    判断Token是否需要刷新
    
    Args:
        jwt_data: JWT payload数据
        
    Returns:
        bool: 是否需要刷新
    """
    try:
        # 获取Token过期时间和签发时间
        exp_timestamp = jwt_data.get('exp')
        iat_timestamp = jwt_data.get('iat')
        
        if not exp_timestamp or not iat_timestamp:
            return False
        
        # 获取当前时间
        now = datetime.now(timezone.utc).timestamp()
        
        # 计算Token总有效期和剩余时间
        total_lifetime = exp_timestamp - iat_timestamp
        remaining_time = exp_timestamp - now
        
        # 如果剩余时间少于总有效期的1/3，需要刷新
        refresh_threshold = total_lifetime / 3
        
        return remaining_time < refresh_threshold and remaining_time > 0
    except Exception as e:
        print(f"判断Token刷新时出错: {str(e)}")
        return False


def setup_jwt_refresh(app):
    """
    设置JWT自动续期中间件
    
    Args:
        app: Flask应用实例
    """
    
    @app.after_request
    def refresh_expiring_jwts(response):
        """
        在每个响应后检查Token是否需要刷新
        如果需要，在响应头中添加新的Token
        """
        try:
            # 只处理成功的请求
            if response.status_code >= 400:
                return response
            
            # 尝试验证当前请求中的JWT
            try:
                verify_jwt_in_request(optional=True)
            except:
                # 如果没有JWT或JWT无效，跳过
                return response
            
            # 获取JWT数据
            jwt_data = get_jwt()
            if not jwt_data:
                return response
            
            # 检查是否需要刷新
            if should_refresh_token(jwt_data):
                # 获取用户身份
                user_identity = get_jwt_identity()
                
                # 创建新的access token
                new_token = create_access_token(
                    identity=user_identity,
                    additional_claims={
                        'username': jwt_data.get('username'),
                        'email': jwt_data.get('email'),
                        'role': jwt_data.get('role')
                    }
                )
                
                # 在响应头中添加新Token
                response.headers['X-New-Token'] = new_token
                
                # 添加CORS头以允许前端读取该响应头
                response.headers['Access-Control-Expose-Headers'] = 'X-New-Token'
                
                print(f"🔄 Token即将过期，已生成新Token (user_id: {user_identity})")
                
        except Exception as e:
            # Token刷新失败不应该影响原有响应
            print(f"Token自动续期失败: {str(e)}")
        
        return response
    
    print("✅ JWT自动续期中间件已启用")
