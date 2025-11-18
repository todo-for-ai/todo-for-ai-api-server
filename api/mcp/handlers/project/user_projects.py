def list_user_projects(arguments):
    """列出用户有权限访问的所有项目"""
    from flask import current_app

    func_start_time = time.time()
    func_id = f"list-user-projects-{int(time.time() * 1000)}-{id(arguments)}"

    current_app.logger.info(f"[LIST_USER_PROJECTS_START] {func_id} Starting to list user projects", extra={
        'func_id': func_id,
        'user_id': g.current_user.id if hasattr(g, 'current_user') and g.current_user else None,
        'arguments': arguments,
        'timestamp': datetime.utcnow().isoformat()
    })

    try:
        # 获取参数
        status_filter = arguments.get('status_filter', 'active')
        include_stats = arguments.get('include_stats', False)

        current_app.logger.debug(f"[LIST_USER_PROJECTS_PARAMS] {func_id} Parameters parsed", extra={
            'func_id': func_id,
            'status_filter': status_filter,
            'include_stats': include_stats,
            'user_id': g.current_user.id
        })

        # 构建查询 - 只返回当前用户拥有的项目
        query_start_time = time.time()
        query = Project.query.filter_by(owner_id=g.current_user.id)

        # 根据状态筛选
        if status_filter == 'active':
            from models.project import ProjectStatus
            query = query.filter_by(status=ProjectStatus.ACTIVE)
        elif status_filter == 'archived':
            from models.project import ProjectStatus
            query = query.filter_by(status=ProjectStatus.ARCHIVED)
        elif status_filter == 'all':
            # 不过滤状态，但排除已删除的项目
            from models.project import ProjectStatus
            query = query.filter(Project.status != ProjectStatus.DELETED)

        # 按最后活动时间排序，如果没有则按创建时间排序
        # MySQL不支持NULLS LAST，使用COALESCE处理NULL值
        from sqlalchemy import func
        projects = query.order_by(
            func.coalesce(Project.last_activity_at, Project.created_at).desc(),
            Project.created_at.desc()
        ).all()

        query_duration = time.time() - query_start_time

        current_app.logger.debug(f"[LIST_USER_PROJECTS_QUERY] {func_id} Projects query completed", extra={
            'func_id': func_id,
            'projects_count': len(projects),
            'query_duration_ms': round(query_duration * 1000, 2),
            'status_filter': status_filter
        })

        # 构建返回数据
        projects_data = []
        for project in projects:
            project_dict = {
                'id': project.id,
                'name': project.name,
                'description': project.description,
                'color': project.color,
                'status': project.status.value if hasattr(project.status, 'value') else getattr(project, 'status', 'active'),
                'github_url': project.github_url,
                'local_url': project.local_url,
                'production_url': project.production_url,
                'project_context': project.project_context,
                'owner_id': project.owner_id,
                'created_at': project.created_at.isoformat(),
                'updated_at': project.updated_at.isoformat(),
                'last_activity_at': project.last_activity_at.isoformat() if project.last_activity_at else None
            }

            # 如果需要包含统计信息
            if include_stats:
                stats_start_time = time.time()

                # 获取任务统计
                from models.task import TaskStatus
                total_tasks = project.tasks.count()
                todo_tasks = project.tasks.filter_by(status=TaskStatus.TODO).count()
                in_progress_tasks = project.tasks.filter_by(status=TaskStatus.IN_PROGRESS).count()
                review_tasks = project.tasks.filter_by(status=TaskStatus.REVIEW).count()
                done_tasks = project.tasks.filter_by(status=TaskStatus.DONE).count()
                cancelled_tasks = project.tasks.filter_by(status=TaskStatus.CANCELLED).count()

                # 计算完成率
                completion_rate = (done_tasks / total_tasks * 100) if total_tasks > 0 else 0

                # 获取上下文规则数量
                context_rules_count = project.context_rules.filter_by(is_active=True).count()

                stats_duration = time.time() - stats_start_time

                project_dict.update({
                    'total_tasks': total_tasks,
                    'pending_tasks': todo_tasks + in_progress_tasks + review_tasks,
                    'completed_tasks': done_tasks,
                    'completion_rate': round(completion_rate, 2),
                    'context_rules_count': context_rules_count,
                    'statistics': {
                        'total_tasks': total_tasks,
                        'todo_tasks': todo_tasks,
                        'in_progress_tasks': in_progress_tasks,
                        'review_tasks': review_tasks,
                        'done_tasks': done_tasks,
                        'cancelled_tasks': cancelled_tasks,
                        'completion_rate': round(completion_rate, 2),
                        'context_rules_count': context_rules_count
                    }
                })

                current_app.logger.debug(f"[LIST_USER_PROJECTS_STATS] {func_id} Stats calculated for project {project.id}", extra={
                    'func_id': func_id,
                    'project_id': project.id,
                    'project_name': project.name,
                    'stats_duration_ms': round(stats_duration * 1000, 2),
                    'total_tasks': total_tasks,
                    'completion_rate': round(completion_rate, 2)
                })

            projects_data.append(project_dict)

        func_duration = time.time() - func_start_time

        result = {
            'projects': projects_data,
            'total': len(projects_data),
            'status_filter': status_filter,
            'include_stats': include_stats,
            'user_id': g.current_user.id
        }

        current_app.logger.info(f"[LIST_USER_PROJECTS_SUCCESS] {func_id} Successfully listed user projects", extra={
            'func_id': func_id,
            'projects_count': len(projects_data),
            'status_filter': status_filter,
            'include_stats': include_stats,
            'func_duration_ms': round(func_duration * 1000, 2),
            'user_id': g.current_user.id
        })

        return result

    except Exception as e:
        func_duration = time.time() - func_start_time
        current_app.logger.error(f"[LIST_USER_PROJECTS_EXCEPTION] {func_id} Exception occurred", extra={
            'func_id': func_id,
            'user_id': g.current_user.id if hasattr(g, 'current_user') and g.current_user else None,
            'func_duration_ms': round(func_duration * 1000, 2),
            'exception_type': type(e).__name__,
            'exception_message': str(e)
        }, exc_info=True)
        return {'error': f'Failed to list user projects: {str(e)}'}
