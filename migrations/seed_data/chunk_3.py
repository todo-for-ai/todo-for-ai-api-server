def create_sample_task_history(tasks):
    """创建示例任务历史"""
    # 为已完成的任务创建历史记录
    completed_task = next((t for t in tasks if t.status == TaskStatus.DONE), None)
    if completed_task:
        TaskHistory.log_action(
            task_id=completed_task.id,
            action=ActionType.CREATED,
            changed_by='system',
            comment='任务创建'
        )
        TaskHistory.log_action(
            task_id=completed_task.id,
            action=ActionType.STATUS_CHANGED,
            changed_by='Frontend-Lead',
            field_name='status',
            old_value='todo',
            new_value='in_progress',
            comment='开始执行任务'
        )
        TaskHistory.log_action(
            task_id=completed_task.id,
            action=ActionType.COMPLETED,
            changed_by='Frontend-Lead',
            field_name='status',
            old_value='in_progress',
            new_value='done',
            comment='任务完成，选择React作为前端框架'
        )
        print(f"✅ 创建任务历史: {completed_task.title}")



def seed_all_data():
    """创建所有测试数据"""
    app = create_app()
    
    with app.app_context():
        print("🌱 开始创建测试数据...")
        print("=" * 50)
        
        # 检查是否已有数据
        if Project.query.first():
            print("⚠️  数据库中已存在数据")
            confirm = input("是否清空现有数据并重新创建？(y/N): ")
            if confirm.lower() != 'y':
                print("操作已取消")
                return
            
            # 清空现有数据
            print("🗑️  清空现有数据...")
            db.session.query(TaskHistory).delete()
            db.session.query(ContextRule).delete()
            db.session.query(Task).delete()
            db.session.query(Project).delete()
            db.session.commit()
        
        # 创建测试数据
        projects = create_sample_projects()
        tasks = create_sample_tasks(projects)
        rules = create_sample_context_rules(projects)
        create_sample_task_history(tasks)
        
        print("=" * 50)
        print("🎉 测试数据创建完成！")
        print(f"📊 统计信息:")
        print(f"  - 项目: {len(projects)} 个")
        print(f"  - 任务: {len(tasks)} 个")
        print(f"  - 上下文规则: {len(rules)} 个")
        print(f"  - 任务历史: {TaskHistory.query.count()} 条")



