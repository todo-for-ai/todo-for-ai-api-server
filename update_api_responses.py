#!/usr/bin/env python3
"""
脚本用于批量更新API响应格式，将所有api_response和api_error调用替换为ApiResponse类的方法
"""

import os
import re
import glob

def update_imports(content):
    """更新导入语句"""
    # 替换导入语句
    content = re.sub(
        r'from \.base import.*api_response.*api_error.*',
        'from .base import ApiResponse, paginate_query, validate_json_request, get_request_args, APIException, handle_api_error',
        content
    )
    content = re.sub(
        r'from api\.base import.*api_response.*api_error.*',
        'from api.base import ApiResponse, handle_api_error',
        content
    )
    return content

def update_api_responses(content):
    """更新api_response调用"""
    # 简单的成功响应
    content = re.sub(
        r'return api_response\(\s*([^,\)]+),\s*"([^"]+)"\s*\)',
        r'return ApiResponse.success(\1, "\2").to_response()',
        content
    )
    
    # 带状态码的响应
    content = re.sub(
        r'return api_response\(\s*([^,\)]+),\s*"([^"]+)",\s*(\d+)\s*\)',
        r'return ApiResponse.success(\1, "\2", \3).to_response()',
        content
    )
    
    # 201创建响应
    content = re.sub(
        r'return api_response\(\s*([^,\)]+),\s*"([^"]+)",\s*201\s*\)',
        r'return ApiResponse.created(\1, "\2").to_response()',
        content
    )
    
    return content

def update_api_errors(content):
    """更新api_error调用"""
    # 简单错误响应
    content = re.sub(
        r'return api_error\("([^"]+)",\s*(\d+)\)',
        r'return ApiResponse.error("\1", \2).to_response()',
        content
    )
    
    # 带错误码的响应
    content = re.sub(
        r'return api_error\("([^"]+)",\s*(\d+),\s*"([^"]+)"\)',
        r'return ApiResponse.error("\1", \2, error_details={"code": "\3"}).to_response()',
        content
    )
    
    # 常见的HTTP状态码快捷方法
    content = re.sub(
        r'return api_error\("([^"]+)",\s*401\)',
        r'return ApiResponse.unauthorized("\1").to_response()',
        content
    )
    content = re.sub(
        r'return api_error\("([^"]+)",\s*403\)',
        r'return ApiResponse.forbidden("\1").to_response()',
        content
    )
    content = re.sub(
        r'return api_error\("([^"]+)",\s*404\)',
        r'return ApiResponse.not_found("\1").to_response()',
        content
    )
    
    return content

def process_file(filepath):
    """处理单个文件"""
    print(f"Processing {filepath}...")
    
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    original_content = content
    
    # 更新导入
    content = update_imports(content)
    
    # 更新API响应
    content = update_api_responses(content)
    
    # 更新API错误
    content = update_api_errors(content)
    
    # 如果内容有变化，写回文件
    if content != original_content:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"Updated {filepath}")
    else:
        print(f"No changes needed for {filepath}")

def main():
    """主函数"""
    # 获取所有API文件
    api_files = glob.glob('api/*.py')
    
    for filepath in api_files:
        if os.path.basename(filepath) in ['__init__.py', 'base.py']:
            continue  # 跳过这些特殊文件
        process_file(filepath)
    
    print("API响应格式更新完成！")

if __name__ == '__main__':
    main()
