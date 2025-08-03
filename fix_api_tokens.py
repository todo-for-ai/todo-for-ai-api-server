#!/usr/bin/env python3
"""
修复api_tokens.py文件，将所有jsonify调用替换为ApiResponse格式
"""

import re

def fix_api_tokens_file():
    with open('api/api_tokens.py', 'r', encoding='utf-8') as f:
        content = f.read()
    
    # 替换模式
    replacements = [
        # 简单错误响应
        (r"return jsonify\(\{'error': '([^']+)'\}\), (\d+)", 
         r"return ApiResponse.error('\1', \2).to_response()"),
        
        # 成功响应 - 列表数据
        (r"return jsonify\(\{\s*'success': True,\s*'data': ([^}]+)\s*\}\)", 
         r"return ApiResponse.success(\1, 'Operation successful').to_response()"),
        
        # 成功响应 - 带消息
        (r"return jsonify\(\{\s*'success': True,\s*'data': ([^,]+),\s*'message': '([^']+)'\s*\}\)", 
         r"return ApiResponse.success(\1, '\2').to_response()"),
        
        # 错误响应 - 带success字段
        (r"return jsonify\(\{\s*'success': False,\s*'error': '([^']+)'\s*\}\), (\d+)", 
         r"return ApiResponse.error('\1', \2).to_response()"),
        
        # 错误响应 - 带success字段和str(e)
        (r"return jsonify\(\{\s*'success': False,\s*'error': str\(e\)\s*\}\), (\d+)", 
         r"return ApiResponse.error(f'Operation failed: {str(e)}', \1).to_response()"),
        
        # 成功响应 - 简单消息
        (r"return jsonify\(\{\s*'success': True,\s*'message': '([^']+)'\s*\}\)", 
         r"return ApiResponse.success(None, '\1').to_response()"),
    ]
    
    # 应用替换
    for pattern, replacement in replacements:
        content = re.sub(pattern, replacement, content, flags=re.MULTILINE | re.DOTALL)
    
    # 特殊处理一些复杂的情况
    # 处理token列表响应
    content = re.sub(
        r"return jsonify\(\{\s*'success': True,\s*'data': token_list\s*\}\)",
        """return ApiResponse.success({
            'items': token_list,
            'pagination': {
                'page': 1,
                'per_page': len(token_list),
                'total': len(token_list),
                'has_prev': False,
                'has_next': False
            }
        }, "API tokens retrieved successfully").to_response()""",
        content
    )
    
    # 处理创建token响应
    content = re.sub(
        r"return jsonify\(\{\s*'success': True,\s*'data': response_data,\s*'message': 'API Token created successfully\. Please save the token as it will not be shown again\.'\s*\}\)",
        """return ApiResponse.created(
            response_data,
            'API Token created successfully. Please save the token as it will not be shown again.'
        ).to_response()""",
        content
    )
    
    # 处理验证token响应
    content = re.sub(
        r"return jsonify\(\{\s*'success': True,\s*'data': \{\s*'token': api_token\.to_dict\(include_sensitive=False\),\s*'user': api_token\.user\.to_public_dict\(\)\s*\}\s*\}\)",
        """return ApiResponse.success({
            'token': api_token.to_dict(include_sensitive=False),
            'user': api_token.user.to_public_dict()
        }, 'Token verified successfully').to_response()""",
        content
    )
    
    # 写回文件
    with open('api/api_tokens.py', 'w', encoding='utf-8') as f:
        f.write(content)
    
    print("API tokens file fixed successfully!")

if __name__ == '__main__':
    fix_api_tokens_file()
