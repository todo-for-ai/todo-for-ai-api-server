def update_project(project_id):
    """更新项目"""
    try:
        current_user = get_current_user()

        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.error("Project not found", 404, error_details={"code": "PROJECT_NOT_FOUND"}).to_response()

        # 权限检查
        if not current_user.can_access_project(project):
            return ApiResponse.error("Access denied", 403).to_response()
        
        # 验证请求数据
        data = validate_json_request(
            optional_fields=['name', 'description', 'color', 'status', 'github_url', 'local_url', 'production_url', 'project_context']
        )
        
        if isinstance(data, tuple):  # 错误响应
            return data
        
        # 检查项目名称是否已被其他项目使用
        if 'name' in data and data['name'] != project.name:
            existing_project = Project.query.filter_by(name=data['name']).first()
            if existing_project:
                return ApiResponse.error("Project name already exists", 409, error_details={"code": "DUPLICATE_NAME"}).to_response()
        
        # 更新项目
        project.update_from_dict(data)

        # 处理状态更新
        if 'status' in data:
            try:
                project.status = ProjectStatus(data['status'])
            except ValueError:
                return api_error(f"Invalid status: {data['status']}", 400)

        # 更新项目最后活动时间
        project.last_activity_at = datetime.utcnow()

        db.session.commit()
        
        return ApiResponse.success(
            project.to_dict(include_stats=True),
            "Project updated successfully"
        ).to_response()
        
    except Exception as e:
        db.session.rollback()
        return api_error(f"Failed to update project: {str(e)}", 500)


@projects_bp.route('/<int:project_id>', methods=['DELETE'])

def delete_project(project_id):
    """删除项目（软删除）"""
    try:
        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.error("Project not found", 404, error_details={"code": "PROJECT_NOT_FOUND"}).to_response()
        
        # 软删除
        project.soft_delete()
        
        return ApiResponse.success(None, "Project deleted successfully", 204).to_response()
        
    except Exception as e:
        db.session.rollback()
        return api_error(f"Failed to delete project: {str(e)}", 500)


@projects_bp.route('/<int:project_id>/archive', methods=['POST'])

def archive_project(project_id):
    """归档项目"""
    try:
        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.error("Project not found", 404, error_details={"code": "PROJECT_NOT_FOUND"}).to_response()
        
        project.archive()
        
        return ApiResponse.success(
            project.to_dict(),
            "Project archived successfully"
        ).to_response()
        
    except Exception as e:
        db.session.rollback()
        return api_error(f"Failed to archive project: {str(e)}", 500)


@projects_bp.route('/<int:project_id>/restore', methods=['POST'])
def restore_project(project_id):
    """恢复项目"""
    try:
        project = Project.query.get(project_id)
        if not project:
            return ApiResponse.error("Project not found", 404, error_details={"code": "PROJECT_NOT_FOUND"}).to_response()
        
        project.restore()
        
        return ApiResponse.success(
            project.to_dict(),
            "Project restored successfully"
        ).to_response()
        
    except Exception as e:
        db.session.rollback()
        return api_error(f"Failed to restore project: {str(e)}", 500)
