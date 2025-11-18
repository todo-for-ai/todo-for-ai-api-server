def clear_all_data():
    """清空所有数据"""
    app = create_app()
    
    with app.app_context():
        print("🗑️  清空所有测试数据...")
        
        confirm = input("⚠️  这将删除所有数据！确认清空？(y/N): ")
        if confirm.lower() != 'y':
            print("操作已取消")
            return
        
        db.session.query(TaskHistory).delete()
        db.session.query(ContextRule).delete()
        db.session.query(Task).delete()
        db.session.query(Project).delete()
        db.session.commit()
        
        print("✅ 所有数据已清空")



def main():
    """主函数"""
    if len(sys.argv) < 2:
        print("用法:")
        print("  python seed_data.py create    - 创建测试数据")
        print("  python seed_data.py clear     - 清空所有数据")
        return
    
    command = sys.argv[1].lower()
    
    if command == 'create':
        seed_all_data()
    elif command == 'clear':
        clear_all_data()
    else:
        print(f"未知命令: {command}")


if __name__ == '__main__':
    main()

