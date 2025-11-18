def submit_task_feedback(arguments):
    """提交任务反馈 - 支持交互式任务"""
    import uuid
    from models import InteractionLog, InteractionType, InteractionStatus
    from flask import current_app

    task_id = arguments.get('task_id')
    project_name = arguments.get('project_name')
    feedback_content = arguments.get('feedback_content')
    status = arguments.get('status')
    ai_identifier = arguments.get('ai_identifier', 'AI Assistant')

    if not all([task_id, project_name, feedback_content, status]):
        return {'error': 'task_id, project_name, feedback_content, and status are required'}

    # 验证和清理输入
    try:
        task_id = validate_integer(task_id, 'task_id')
    except ValueError as e:
        return {'error': str(e)}

    project_name = sanitize_input(project_name)
    feedback_content = sanitize_input(feedback_content)
    ai_identifier = sanitize_input(ai_identifier)

    # 验证状态值
    valid_statuses = ['in_progress', 'review', 'done', 'cancelled', 'waiting_human_feedback']
    if status not in valid_statuses:
        return {'error': f'Invalid status. Must be one of: {", ".join(valid_statuses)}'}

    # 验证任务存在并属于指定项目
    task = Task.query.get(task_id)
    if not task:
        return {'error': f'Task with ID {task_id} not found'}

    project = Project.query.get(task.project_id)
    if not project or project.name != project_name:
        return {'error': f'Task {task_id} does not belong to project "{project_name}"'}

    # 检查权限 - 只能修改自己创建的任务或自己项目中的任务
    if task.creator_id != g.current_user.id and project.owner_id != g.current_user.id:
        return {'error': 'Access denied: You can only modify your own tasks'}

    # 跟踪状态变更
    old_status = task.status
    status_changed = str(old_status) != str(status)

    # 检查是否为交互式任务
    is_interactive_task = task.is_interactive

    # 生成或使用现有的交互会话ID
    session_id = task.interaction_session_id
    if not session_id:
        session_id = str(uuid.uuid4())
        task.interaction_session_id = session_id

    current_app.logger.info(f"[SUBMIT_TASK_FEEDBACK] Processing feedback for task {task_id}", extra={
        'task_id': task_id,
        'is_interactive': is_interactive_task,
        'session_id': session_id,
        'status': status,
        'ai_identifier': ai_identifier
    })

    # 更新任务基本信息
    task.feedback_content = feedback_content
    task.feedback_at = datetime.utcnow()

    # 处理交互式任务逻辑
    if is_interactive_task and status != 'cancelled':
        # 创建AI反馈记录
        interaction_log = InteractionLog.create_ai_feedback(
            task_id=task_id,
            session_id=session_id,
            content=feedback_content,
            metadata={
                'ai_identifier': ai_identifier,
                'original_status': str(old_status),
                'requested_status': status
            }
        )

        # 如果AI请求完成任务，设置为等待人工反馈状态
        if status == 'done':
            task.status = 'waiting_human_feedback'
            task.ai_waiting_feedback = True
            current_app.logger.info(f"[INTERACTIVE_TASK] Task {task_id} set to waiting_human_feedback")
        else:
            # 对于其他状态，直接更新
            task.status = status
            task.ai_waiting_feedback = False
    else:
        # 非交互式任务，直接更新状态
        task.status = status
        task.ai_waiting_feedback = False

    # 更新项目最后活动时间
    project.last_activity_at = datetime.utcnow()

    try:
        db.session.commit()
        current_app.logger.info(f"[SUBMIT_TASK_FEEDBACK] Successfully updated task {task_id}")
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"[SUBMIT_TASK_FEEDBACK] Failed to update task {task_id}: {str(e)}")
        return {'error': f'Failed to submit feedback: {str(e)}'}

    # 记录用户活跃度
    user_id = None
    if task.creator_id:
        user_id = task.creator_id
    elif project.owner_id:
        user_id = project.owner_id

    if user_id:
        from models import UserActivity
        try:
            if status_changed:
                UserActivity.record_activity(user_id, 'task_status_changed')
                # 如果任务状态变为完成，额外记录完成任务活跃度
                if task.status == 'done':
                    UserActivity.record_activity(user_id, 'task_completed')
            else:
                UserActivity.record_activity(user_id, 'task_updated')
        except Exception as e:
            print(f"Warning: Failed to record user activity: {str(e)}")

    # 构建返回结果
    result = {
        'task_id': task_id,
        'project_name': project_name,
        'status': task.status.value if hasattr(task.status, 'value') else task.status,
        'feedback_submitted': True,
        'feedback_content': feedback_content,
        'ai_identifier': ai_identifier,
        'timestamp': datetime.utcnow().isoformat(),
        'is_interactive': is_interactive_task,
        'session_id': session_id
    }

    # 如果是交互式任务且AI在等待反馈，添加等待信息
    if is_interactive_task and task.ai_waiting_feedback:
        result['waiting_human_feedback'] = True
        result['message'] = 'Task feedback submitted. Waiting for human confirmation or additional instructions.'

    return result
