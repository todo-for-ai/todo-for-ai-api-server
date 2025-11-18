def create_app():
    """创建 Flask 应用"""
    app = Flask(__name__)
    app.config.from_object(config['development'])
    db.init_app(app)
    return app



def create_sample_projects():
    """创建示例项目"""
    projects_data = [
        {
            'name': 'AI 助手开发',
            'description': '开发一个智能的AI助手系统，支持多种任务类型和上下文理解',
            'color': '#1890ff',
            'created_by': 'system'
        },
        {
            'name': '网站重构项目',
            'description': '对现有网站进行全面重构，提升用户体验和性能',
            'color': '#52c41a',
            'created_by': 'system'
        },
        {
            'name': '数据分析平台',
            'description': '构建企业级数据分析平台，支持实时数据处理和可视化',
            'color': '#722ed1',
            'created_by': 'system'
        },
        {
            'name': '移动应用开发',
            'description': '开发跨平台移动应用，支持iOS和Android',
            'color': '#fa8c16',
            'created_by': 'system'
        }
    ]
    
    projects = []
    for data in projects_data:
        project = Project.create(**data)
        projects.append(project)
        print(f"✅ 创建项目: {project.name}")
    
    db.session.commit()
    return projects



