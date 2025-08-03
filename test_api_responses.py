#!/usr/bin/env python3
"""
测试所有API接口的响应格式
验证是否符合新的统一格式：{code, message, data, timestamp, path}
"""

import requests
import json
import sys
from datetime import datetime

# API配置
BASE_URL = "http://127.0.0.1:50110/todo-for-ai/api/v1"
TOKEN = "8y3QmMqbZvKMxXDN9BAYt2L2CtQyummfNEsN49_PD68"

headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}

def test_response_format(response, endpoint_name):
    """验证响应格式是否符合标准"""
    print(f"\n🔍 测试 {endpoint_name}")
    print(f"状态码: {response.status_code}")
    
    try:
        data = response.json()
        print(f"响应内容: {json.dumps(data, indent=2, ensure_ascii=False)}")
        
        # 验证必需字段
        required_fields = ['code', 'message']
        missing_fields = []
        
        for field in required_fields:
            if field not in data:
                missing_fields.append(field)
        
        if missing_fields:
            print(f"❌ 缺少必需字段: {missing_fields}")
            return False
        
        # 验证字段类型
        if not isinstance(data['code'], int):
            print(f"❌ code字段应该是整数，实际是: {type(data['code'])}")
            return False
            
        if not isinstance(data['message'], str):
            print(f"❌ message字段应该是字符串，实际是: {type(data['message'])}")
            return False
        
        # 验证可选字段
        optional_fields = ['data', 'timestamp', 'path']
        for field in optional_fields:
            if field in data:
                print(f"✅ 包含可选字段: {field}")
        
        print(f"✅ 响应格式正确")
        return True
        
    except json.JSONDecodeError:
        print(f"❌ 响应不是有效的JSON")
        print(f"原始响应: {response.text}")
        return False

def test_projects_api():
    """测试项目相关API"""
    print("\n" + "="*50)
    print("📁 测试项目API")
    print("="*50)
    
    # 1. 获取项目列表
    response = requests.get(f"{BASE_URL}/projects", headers=headers)
    test_response_format(response, "GET /projects")
    
    # 2. 创建项目
    project_data = {
        "name": f"API测试项目_{datetime.now().strftime('%H%M%S')}",
        "description": "用于测试API响应格式的项目"
    }
    response = requests.post(f"{BASE_URL}/projects", headers=headers, json=project_data)
    result = test_response_format(response, "POST /projects")
    
    project_id = None
    if result and response.status_code == 201:
        try:
            project_id = response.json().get('data', {}).get('id')
            print(f"创建的项目ID: {project_id}")
        except:
            pass
    
    # 3. 获取单个项目
    if project_id:
        response = requests.get(f"{BASE_URL}/projects/{project_id}", headers=headers)
        test_response_format(response, f"GET /projects/{project_id}")
        
        # 4. 更新项目
        update_data = {"description": "更新后的描述"}
        response = requests.put(f"{BASE_URL}/projects/{project_id}", headers=headers, json=update_data)
        test_response_format(response, f"PUT /projects/{project_id}")
    
    return project_id

def test_tasks_api(project_id=None):
    """测试任务相关API"""
    print("\n" + "="*50)
    print("📋 测试任务API")
    print("="*50)
    
    # 1. 获取任务列表
    response = requests.get(f"{BASE_URL}/tasks", headers=headers)
    test_response_format(response, "GET /tasks")
    
    # 2. 创建任务（如果有项目ID）
    task_id = None
    if project_id:
        task_data = {
            "title": f"API测试任务_{datetime.now().strftime('%H%M%S')}",
            "content": "用于测试API响应格式的任务",
            "project_id": project_id
        }
        response = requests.post(f"{BASE_URL}/tasks", headers=headers, json=task_data)
        result = test_response_format(response, "POST /tasks")
        
        if result and response.status_code == 201:
            try:
                task_id = response.json().get('data', {}).get('id')
                print(f"创建的任务ID: {task_id}")
            except:
                pass
    
    # 3. 获取单个任务
    if task_id:
        response = requests.get(f"{BASE_URL}/tasks/{task_id}", headers=headers)
        test_response_format(response, f"GET /tasks/{task_id}")
    
    return task_id

def test_context_rules_api():
    """测试上下文规则API"""
    print("\n" + "="*50)
    print("📝 测试上下文规则API")
    print("="*50)
    
    # 1. 获取上下文规则列表
    response = requests.get(f"{BASE_URL}/context-rules", headers=headers)
    test_response_format(response, "GET /context-rules")

def test_pins_api(project_id=None):
    """测试Pin相关API"""
    print("\n" + "="*50)
    print("📌 测试Pin API")
    print("="*50)
    
    if project_id:
        # 1. Pin项目
        response = requests.post(f"{BASE_URL}/pins/projects/{project_id}", headers=headers)
        test_response_format(response, f"POST /pins/projects/{project_id}")
        
        # 2. 获取Pin状态
        response = requests.get(f"{BASE_URL}/pins/projects/{project_id}/status", headers=headers)
        test_response_format(response, f"GET /pins/projects/{project_id}/status")
        
        # 3. 获取Pin统计
        response = requests.get(f"{BASE_URL}/pins/stats", headers=headers)
        test_response_format(response, "GET /pins/stats")

def test_tokens_api():
    """测试Token相关API"""
    print("\n" + "="*50)
    print("🔑 测试Token API")
    print("="*50)

    # 1. 获取Token列表
    response = requests.get(f"{BASE_URL}/tokens", headers=headers)
    test_response_format(response, "GET /tokens")

def test_api_tokens_api():
    """测试API Token管理API"""
    print("\n" + "="*50)
    print("🔐 测试API Token管理API")
    print("="*50)

    # 1. 获取API Token列表
    response = requests.get(f"{BASE_URL}/api-tokens", headers=headers)
    test_response_format(response, "GET /api-tokens")

def test_dashboard_api():
    """测试Dashboard API"""
    print("\n" + "="*50)
    print("📊 测试Dashboard API")
    print("="*50)

    # 1. 获取Dashboard数据
    response = requests.get(f"{BASE_URL}/dashboard", headers=headers)
    test_response_format(response, "GET /dashboard")

def test_user_settings_api():
    """测试用户设置API"""
    print("\n" + "="*50)
    print("⚙️ 测试用户设置API")
    print("="*50)

    # 1. 获取用户设置
    response = requests.get(f"{BASE_URL}/user-settings", headers=headers)
    test_response_format(response, "GET /user-settings")

def test_docs_api():
    """测试文档API"""
    print("\n" + "="*50)
    print("📚 测试文档API")
    print("="*50)

    # 1. 获取API文档
    response = requests.get(f"{BASE_URL}/docs", headers=headers)
    test_response_format(response, "GET /docs")

def main():
    """主测试函数"""
    print("🚀 开始API响应格式测试")
    print(f"测试时间: {datetime.now()}")
    print(f"API基础URL: {BASE_URL}")
    
    try:
        # 测试各个API模块
        project_id = test_projects_api()
        task_id = test_tasks_api(project_id)
        test_context_rules_api()
        test_pins_api(project_id)
        test_tokens_api()
        test_api_tokens_api()
        test_dashboard_api()
        test_user_settings_api()
        test_docs_api()
        
        print("\n" + "="*50)
        print("✅ API响应格式测试完成")
        print("="*50)
        
    except Exception as e:
        print(f"\n❌ 测试过程中出现错误: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
