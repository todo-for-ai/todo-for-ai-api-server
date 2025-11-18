def wait_for_human_feedback(arguments):
    """等待人工反馈 - 用于交互式任务"""
    import time
    from models import InteractionLog, InteractionType, InteractionStatus
    from flask import current_app

    func_start_time = time.time()
    func_id = f"wait-for-human-feedback-{int(time.time() * 1000)}-{id(arguments)}"

    current_app.logger.info(f"[WAIT_FOR_HUMAN_FEEDBACK_START] {func_id} Function started", extra={
        'func_id': func_id,
        'arguments': arguments,
        'user_id': g.current_user.id if hasattr(g, 'current_user') and g.current_user else None,
        'timestamp': datetime.utcnow().isoformat()
    })

    task_id = arguments.get('task_id')
    session_id = arguments.get('session_id')
    timeout_seconds = arguments.get('timeout_seconds', 3600)  # Default 1 hour
    poll_interval_seconds = arguments.get('poll_interval_seconds', 30)  # Default 30 seconds

    current_app.logger.debug(f"[WAIT_FOR_HUMAN_FEEDBACK_ARGS] {func_id} Arguments parsed", extra={
        'func_id': func_id,
        'task_id': task_id,
        'session_id': session_id,
        'timeout_seconds': timeout_seconds,
        'poll_interval_seconds': poll_interval_seconds
    })

    if not all([task_id, session_id]):
        current_app.logger.warning(f"[WAIT_FOR_HUMAN_FEEDBACK_ERROR] {func_id} Missing required arguments")
        return {'error': 'task_id and session_id are required'}

    # 验证输入
    try:
        task_id = validate_integer(task_id, 'task_id')
        timeout_seconds = max(30, min(7200, int(timeout_seconds)))  # 30秒到2小时
        poll_interval_seconds = max(10, min(300, int(poll_interval_seconds)))  # 10秒到5分钟
    except (ValueError, TypeError) as e:
        return {'error': f'Invalid input: {str(e)}'}

    session_id = sanitize_input(session_id)

    # 验证任务存在
    task = Task.query.get(task_id)
    if not task:
        return {'error': f'Task with ID {task_id} not found'}

    # 检查权限
    if task.creator_id != g.current_user.id:
        project = Project.query.get(task.project_id)
        if not project or project.owner_id != g.current_user.id:
            return {'error': 'Access denied: You can only access your own tasks'}

    # 验证任务是交互式的且在等待反馈
    if not task.is_interactive:
        return {'error': 'Task is not interactive'}

    if not task.ai_waiting_feedback:
        return {'error': 'Task is not waiting for human feedback'}

    if task.interaction_session_id != session_id:
        return {'error': 'Invalid session ID'}

    # 开始轮询等待人工反馈
    end_time = time.time() + timeout_seconds
    poll_count = 0

    current_app.logger.info(f"[WAIT_FOR_HUMAN_FEEDBACK_POLLING_START] {func_id} Starting polling loop", extra={
        'func_id': func_id,
        'task_id': task_id,
        'session_id': session_id,
        'timeout_seconds': timeout_seconds,
        'poll_interval_seconds': poll_interval_seconds
    })

    try:
        while time.time() < end_time:
            poll_count += 1
            poll_start_time = time.time()

            current_app.logger.debug(f"[WAIT_FOR_HUMAN_FEEDBACK_POLL] {func_id} Poll #{poll_count} started", extra={
                'func_id': func_id,
                'poll_count': poll_count,
                'remaining_time': end_time - time.time()
            })

            # 刷新任务状态
            db.session.refresh(task)

            # 检查是否有新的人工响应
            human_responses = InteractionLog.query.filter(
                InteractionLog.session_id == session_id,
                InteractionLog.interaction_type == InteractionType.HUMAN_RESPONSE,
                InteractionLog.created_at > datetime.utcnow() - timedelta(seconds=timeout_seconds + 60)
            ).order_by(InteractionLog.created_at.desc()).all()

            poll_duration = time.time() - poll_start_time

            current_app.logger.debug(f"[WAIT_FOR_HUMAN_FEEDBACK_POLL_RESULT] {func_id} Poll #{poll_count} completed", extra={
                'func_id': func_id,
                'poll_count': poll_count,
                'poll_duration_ms': round(poll_duration * 1000, 2),
                'human_responses_count': len(human_responses),
                'ai_waiting_feedback': task.ai_waiting_feedback
            })

            # 如果任务不再等待反馈，说明有人工响应
            if not task.ai_waiting_feedback or human_responses:
                latest_response = human_responses[0] if human_responses else None

                func_duration = time.time() - func_start_time

                current_app.logger.info(f"[WAIT_FOR_HUMAN_FEEDBACK_SUCCESS] {func_id} Received human feedback", extra={
                    'func_id': func_id,
                    'task_id': task_id,
                    'session_id': session_id,
                    'poll_count': poll_count,
                    'func_duration_ms': round(func_duration * 1000, 2),
                    'response_status': latest_response.status.value if latest_response else 'unknown'
                })

                result = {
                    'task_id': task_id,
                    'session_id': session_id,
                    'human_feedback_received': True,
                    'poll_count': poll_count,
                    'wait_duration_seconds': round(func_duration, 2),
                    'timeout': False,
                    'task_status': task.status.value if hasattr(task.status, 'value') else task.status,
                    'ai_waiting_feedback': task.ai_waiting_feedback
                }

                if latest_response:
                    result['human_response'] = {
                        'content': latest_response.content,
                        'status': latest_response.status.value if hasattr(latest_response.status, 'value') else latest_response.status,
                        'created_at': latest_response.created_at.isoformat(),
                        'created_by': latest_response.created_by
                    }

                    # 根据人工响应状态决定下一步行动
                    if latest_response.status == InteractionStatus.COMPLETED:
                        result['action'] = 'task_completed'
                        result['message'] = 'Task has been marked as completed by human reviewer.'
                    elif latest_response.status == InteractionStatus.CONTINUED:
                        result['action'] = 'continue_task'
                        result['message'] = 'Human has provided additional instructions. Continue working on the task.'
                        result['additional_instructions'] = latest_response.content
                    else:
                        result['action'] = 'pending'
                        result['message'] = 'Human response received but status is pending.'

                return result

            # 没有收到人工反馈，检查是否还有时间继续轮询
            remaining_time = end_time - time.time()
            if remaining_time <= 0:
                break

            # 等待下一次轮询
            sleep_time = min(poll_interval_seconds, remaining_time)
            if sleep_time > 0:
                current_app.logger.debug(f"[WAIT_FOR_HUMAN_FEEDBACK_SLEEP] {func_id} Sleeping before next poll", extra={
                    'func_id': func_id,
                    'poll_count': poll_count,
                    'sleep_time': sleep_time,
                    'remaining_time': remaining_time
                })
                time.sleep(sleep_time)

        # 超时，没有收到人工反馈
        func_duration = time.time() - func_start_time

        current_app.logger.info(f"[WAIT_FOR_HUMAN_FEEDBACK_TIMEOUT] {func_id} Wait timed out", extra={
            'func_id': func_id,
            'task_id': task_id,
            'session_id': session_id,
            'poll_count': poll_count,
            'func_duration_ms': round(func_duration * 1000, 2),
            'timeout_seconds': timeout_seconds
        })

        return {
            'task_id': task_id,
            'session_id': session_id,
            'human_feedback_received': False,
            'poll_count': poll_count,
            'wait_duration_seconds': round(func_duration, 2),
            'timeout': True,
            'timeout_seconds': timeout_seconds,
            'task_status': task.status.value if hasattr(task.status, 'value') else task.status,
            'ai_waiting_feedback': task.ai_waiting_feedback,
            'message': 'Timeout waiting for human feedback. Task remains in waiting state.'
        }

    except Exception as e:
        func_duration = time.time() - func_start_time
        current_app.logger.error(f"[WAIT_FOR_HUMAN_FEEDBACK_EXCEPTION] {func_id} Exception occurred", extra={
            'func_id': func_id,
            'task_id': task_id,
            'session_id': session_id,
            'func_duration_ms': round(func_duration * 1000, 2),
            'poll_count': poll_count,
            'exception_type': type(e).__name__,
            'exception_message': str(e)
        }, exc_info=True)
        return {'error': f'Failed to wait for human feedback: {str(e)}'}
