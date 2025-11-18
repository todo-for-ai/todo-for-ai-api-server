def update_task(arguments):
    """更新现有任务"""
    from flask import current_app
    import requests

    func_start_time = time.time()
    func_id = f"update-task-{int(time.time() * 1000)}-{id(arguments)}"

    current_app.logger.info(f"[UPDATE_TASK_START] {func_id} Function started", extra={
        'func_id': func_id,
        'arguments': arguments,
        'user_id': g.current_user.id if hasattr(g, 'current_user') and g.current_user else None,
        'timestamp': datetime.utcnow().isoformat()
    })

    task_id = arguments.get('task_id')

    if not task_id:
        current_app.logger.warning(f"[UPDATE_TASK_ERROR] {func_id} Missing required task_id")
        return {'error': 'task_id is required'}

    # 验证task_id是整数
    try:
        task_id = validate_integer(task_id, 'task_id')
    except ValueError as e:
        current_app.logger.warning(f"[UPDATE_TASK_ERROR] {func_id} Invalid task_id: {str(e)}")
        return {'error': str(e)}

    current_app.logger.debug(f"[UPDATE_TASK_ARGS] {func_id} Arguments parsed", extra={
        'func_id': func_id,
        'task_id': task_id,
        'has_title': 'title' in arguments,
        'has_content': 'content' in arguments,
        'has_status': 'status' in arguments,
        'has_priority': 'priority' in arguments,
        'has_due_date': 'due_date' in arguments
    })

    # 构建更新数据
    update_data = {}

    # 清理和验证输入
    if 'title' in arguments and arguments['title']:
        update_data['title'] = sanitize_input(arguments['title'])

    if 'content' in arguments and arguments['content'] is not None:
        update_data['content'] = sanitize_input(arguments['content'])

    if 'status' in arguments and arguments['status']:
        valid_statuses = ['todo', 'in_progress', 'review', 'done', 'cancelled']
        status = arguments['status']
        if status not in valid_statuses:
            return {'error': f'Invalid status. Must be one of: {", ".join(valid_statuses)}'}
        update_data['status'] = status

    if 'priority' in arguments and arguments['priority']:
        valid_priorities = ['low', 'medium', 'high', 'urgent']
        priority = arguments['priority']
        if priority not in valid_priorities:
            return {'error': f'Invalid priority. Must be one of: {", ".join(valid_priorities)}'}
        update_data['priority'] = priority

    if 'due_date' in arguments and arguments['due_date']:
        # 验证日期格式
        try:
            datetime.strptime(arguments['due_date'], '%Y-%m-%d')
            update_data['due_date'] = arguments['due_date']
        except ValueError:
            return {'error': 'Invalid due_date format. Use YYYY-MM-DD'}

    if 'completion_rate' in arguments and arguments['completion_rate'] is not None:
        try:
            completion_rate = float(arguments['completion_rate'])
            if completion_rate < 0 or completion_rate > 100:
                return {'error': 'completion_rate must be between 0 and 100'}
            update_data['completion_rate'] = completion_rate
        except (ValueError, TypeError):
            return {'error': 'completion_rate must be a valid number'}

    if 'tags' in arguments and arguments['tags'] is not None:
        if isinstance(arguments['tags'], list):
            update_data['tags'] = [sanitize_input(tag) for tag in arguments['tags'] if tag]
        else:
            return {'error': 'tags must be an array of strings'}

    # 如果没有提供任何要更新的字段
    if not update_data:
        return {'error': 'At least one field must be provided for update'}

    current_app.logger.debug(f"[UPDATE_TASK_DATA] {func_id} Update data prepared", extra={
        'func_id': func_id,
        'task_id': task_id,
        'update_fields': list(update_data.keys()),
        'update_data_size': len(str(update_data))
    })

    try:
        # 直接调用后端 tasks API
        # 构建内部 API 请求头，包含当前用户的认证信息
        headers = {
            'Content-Type': 'application/json',
            'Authorization': request.headers.get('Authorization')  # 传递原始的认证头
        }

        # 获取当前应用的基础 URL
        base_url = request.host_url.rstrip('/')
        tasks_api_url = f"{base_url}/api/tasks/{task_id}"

        current_app.logger.debug(f"[UPDATE_TASK_API_CALL] {func_id} Calling tasks API", extra={
            'func_id': func_id,
            'api_url': tasks_api_url,
            'headers': {k: v if k != 'Authorization' else 'Bearer ***' for k, v in headers.items()},
            'update_data': update_data
        })

        api_call_start_time = time.time()

        # 调用内部 tasks API
        response = requests.put(
            tasks_api_url,
            json=update_data,
            headers=headers,
            timeout=30
        )

        api_call_duration = time.time() - api_call_start_time

        current_app.logger.debug(f"[UPDATE_TASK_API_RESPONSE] {func_id} Tasks API response received", extra={
            'func_id': func_id,
            'api_call_duration_ms': round(api_call_duration * 1000, 2),
            'status_code': response.status_code,
            'has_response_data': bool(response.content)
        })

        if response.status_code == 200:
            result = response.json()

            # 检查响应格式
            if 'data' in result:
                task_data = result['data']
            else:
                task_data = result

            func_duration = time.time() - func_start_time

            current_app.logger.info(f"[UPDATE_TASK_SUCCESS] {func_id} Task updated successfully", extra={
                'func_id': func_id,
                'task_id': task_id,
                'func_duration_ms': round(func_duration * 1000, 2),
                'updated_fields': list(update_data.keys()),
                'task_title': task_data.get('title', 'Unknown')
            })

            return task_data
        else:
            # API 调用失败
            error_data = {}
            try:
                error_data = response.json()
            except:
                error_data = {'error': f'HTTP {response.status_code}: {response.text}'}

            current_app.logger.warning(f"[UPDATE_TASK_API_ERROR] {func_id} Tasks API returned error", extra={
                'func_id': func_id,
                'task_id': task_id,
                'status_code': response.status_code,
                'error_data': error_data
            })

            return {'error': error_data.get('error', f'Failed to update task: HTTP {response.status_code}')}

    except requests.RequestException as e:
        func_duration = time.time() - func_start_time
        current_app.logger.error(f"[UPDATE_TASK_REQUEST_ERROR] {func_id} Request exception occurred", extra={
            'func_id': func_id,
            'task_id': task_id,
            'func_duration_ms': round(func_duration * 1000, 2),
            'exception_type': type(e).__name__,
            'exception_message': str(e)
        }, exc_info=True)
        return {'error': f'Failed to call tasks API: {str(e)}'}

    except Exception as e:
        func_duration = time.time() - func_start_time
        current_app.logger.error(f"[UPDATE_TASK_EXCEPTION] {func_id} Exception occurred", extra={
            'func_id': func_id,
            'task_id': task_id,
            'func_duration_ms': round(func_duration * 1000, 2),
            'exception_type': type(e).__name__,
            'exception_message': str(e)
        }, exc_info=True)
        return {'error': f'Failed to update task: {str(e)}'}
