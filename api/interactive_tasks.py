"""
交互式任务API接口
"""

from flask import Blueprint, request, jsonify, g
from models import db, Task, InteractionLog, InteractionType, InteractionStatus
from api.base import handle_api_error
from core.github_config import require_auth
from datetime import datetime
import time

interactive_bp = Blueprint('interactive', __name__)


@interactive_bp.route('/tasks/<int:task_id>/human-feedback', methods=['POST'])
@require_auth
def submit_human_feedback(task_id):
    """提交人工反馈"""
    from flask import current_app
    
    func_start_time = time.time()
    func_id = f"submit-human-feedback-{int(time.time() * 1000)}-{task_id}"
    
    current_app.logger.info(f"[SUBMIT_HUMAN_FEEDBACK_START] {func_id} Function started", extra={
        'func_id': func_id,
        'task_id': task_id,
        'user_id': g.current_user.id if hasattr(g, 'current_user') and g.current_user else None,
        'timestamp': datetime.utcnow().isoformat()
    })
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        feedback_content = data.get('feedback_content')
        action = data.get('action')  # 'complete' or 'continue'
        session_id = data.get('session_id')
        
        if not all([feedback_content, action, session_id]):
            return jsonify({'error': 'feedback_content, action, and session_id are required'}), 400
        
        if action not in ['complete', 'continue']:
            return jsonify({'error': 'action must be "complete" or "continue"'}), 400
        
        # 验证任务存在
        task = Task.query.get(task_id)
        if not task:
            return jsonify({'error': f'Task with ID {task_id} not found'}), 404
        
        # 检查权限 - 只能对自己项目中的任务提供反馈
        from models import Project
        project = Project.query.get(task.project_id)
        if not project or project.owner_id != g.current_user.id:
            return jsonify({'error': 'Access denied: You can only provide feedback on tasks in your own projects'}), 403
        
        # 验证任务是交互式的且在等待反馈
        if not task.is_interactive:
            return jsonify({'error': 'Task is not interactive'}), 400
        
        if not task.ai_waiting_feedback:
            return jsonify({'error': 'Task is not waiting for human feedback'}), 400
        
        if task.interaction_session_id != session_id:
            return jsonify({'error': 'Invalid session ID'}), 400
        
        # 确定交互状态
        interaction_status = InteractionStatus.COMPLETED if action == 'complete' else InteractionStatus.CONTINUED
        
        # 创建人工响应记录
        interaction_log = InteractionLog.create_human_response(
            task_id=task_id,
            session_id=session_id,
            content=feedback_content,
            status=interaction_status,
            metadata={
                'action': action,
                'user_id': g.current_user.id,
                'user_email': getattr(g.current_user, 'email', 'unknown')
            },
            created_by=f"user_{g.current_user.id}"
        )
        
        # 更新任务状态
        if action == 'complete':
            # 人工确认任务完成
            task.status = 'done'
            task.ai_waiting_feedback = False
            task.completed_at = datetime.utcnow()
            current_app.logger.info(f"[SUBMIT_HUMAN_FEEDBACK] Task {task_id} marked as completed by human")
        else:
            # 人工要求继续执行
            task.status = 'in_progress'
            task.ai_waiting_feedback = False
            # 可以选择清除或保留session_id，这里选择保留以便继续交互
            current_app.logger.info(f"[SUBMIT_HUMAN_FEEDBACK] Task {task_id} set back to in_progress for continuation")
        
        # 更新项目最后活动时间
        project.last_activity_at = datetime.utcnow()
        
        try:
            db.session.commit()
            current_app.logger.info(f"[SUBMIT_HUMAN_FEEDBACK] Successfully processed human feedback for task {task_id}")
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"[SUBMIT_HUMAN_FEEDBACK] Failed to process human feedback for task {task_id}: {str(e)}")
            return jsonify({'error': f'Failed to submit feedback: {str(e)}'}), 500
        
        # 记录用户活跃度
        from models import UserActivity
        try:
            UserActivity.record_activity(g.current_user.id, 'task_feedback_provided')
            if action == 'complete':
                UserActivity.record_activity(g.current_user.id, 'task_completed')
        except Exception as e:
            current_app.logger.warning(f"Failed to record user activity: {str(e)}")
        
        func_duration = time.time() - func_start_time
        
        result = {
            'task_id': task_id,
            'session_id': session_id,
            'action': action,
            'feedback_content': feedback_content,
            'task_status': task.status.value if hasattr(task.status, 'value') else task.status,
            'ai_waiting_feedback': task.ai_waiting_feedback,
            'interaction_log_id': interaction_log.id,
            'timestamp': datetime.utcnow().isoformat(),
            'processing_duration_ms': round(func_duration * 1000, 2)
        }
        
        current_app.logger.info(f"[SUBMIT_HUMAN_FEEDBACK_SUCCESS] {func_id} Human feedback processed successfully", extra={
            'func_id': func_id,
            'task_id': task_id,
            'action': action,
            'func_duration_ms': round(func_duration * 1000, 2),
            'result': result
        })
        
        return jsonify(result), 200
        
    except Exception as e:
        func_duration = time.time() - func_start_time
        current_app.logger.error(f"[SUBMIT_HUMAN_FEEDBACK_EXCEPTION] {func_id} Exception occurred", extra={
            'func_id': func_id,
            'task_id': task_id,
            'func_duration_ms': round(func_duration * 1000, 2),
            'exception_type': type(e).__name__,
            'exception_message': str(e)
        }, exc_info=True)
        return handle_api_error(e)


@interactive_bp.route('/tasks/<int:task_id>/interaction-status', methods=['GET'])
@require_auth
def get_interaction_status(task_id):
    """获取任务的交互状态"""
    try:
        # 验证任务存在
        task = Task.query.get(task_id)
        if not task:
            return jsonify({'error': f'Task with ID {task_id} not found'}), 404
        
        # 检查权限
        from models import Project
        project = Project.query.get(task.project_id)
        if not project or project.owner_id != g.current_user.id:
            return jsonify({'error': 'Access denied: You can only access tasks in your own projects'}), 403
        
        result = {
            'task_id': task_id,
            'is_interactive': task.is_interactive,
            'ai_waiting_feedback': task.ai_waiting_feedback,
            'interaction_session_id': task.interaction_session_id,
            'task_status': task.status.value if hasattr(task.status, 'value') else task.status,
            'feedback_content': task.feedback_content,
            'feedback_at': task.feedback_at.isoformat() if task.feedback_at else None
        }
        
        return jsonify(result), 200
        
    except Exception as e:
        return handle_api_error(e)


@interactive_bp.route('/tasks/<int:task_id>/interaction-history', methods=['GET'])
@require_auth
def get_interaction_history(task_id):
    """获取任务的交互历史"""
    try:
        # 验证任务存在
        task = Task.query.get(task_id)
        if not task:
            return jsonify({'error': f'Task with ID {task_id} not found'}), 404
        
        # 检查权限
        from models import Project
        project = Project.query.get(task.project_id)
        if not project or project.owner_id != g.current_user.id:
            return jsonify({'error': 'Access denied: You can only access tasks in your own projects'}), 403
        
        # 获取交互历史
        interactions = InteractionLog.get_task_interactions(task_id)
        
        interaction_data = []
        for interaction in interactions:
            interaction_dict = interaction.to_dict()
            interaction_data.append(interaction_dict)
        
        result = {
            'task_id': task_id,
            'total_interactions': len(interaction_data),
            'interactions': interaction_data
        }
        
        return jsonify(result), 200
        
    except Exception as e:
        return handle_api_error(e)
