"""
MCP (Model Context Protocol) API 路由
"""
from flask import Blueprint
from ..base import ApiResponse

def register_routes(bp: Blueprint):
    """注册MCP路由"""

    @bp.route('/', methods=['GET'])
    def mcp_index():
        """MCP服务状态"""
        return ApiResponse.success({
            "service": "MCP",
            "version": "1.0.0",
            "status": "running"
        }).to_response()

    @bp.route('/tools', methods=['GET'])
    def list_tools():
        """列出可用工具"""
        return ApiResponse.success([]).to_response()
