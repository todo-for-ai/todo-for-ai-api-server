"""Add agent_sandboxes, sandbox_executions, sandbox_violations tables for
Agent execution isolation (Increment 85: Agent collaboration sandbox).

Revision: add_agent_sandboxes
"""

from models import db


def migrate():
    """Create missing sandbox tables via SQLAlchemy metadata."""
    try:
        print("Creating Agent sandbox tables...")
        db.create_all()
        print("Agent sandbox tables created successfully.")
        return True
    except Exception as e:
        print(f"Migration failed: {e}")
        db.session.rollback()
        return False


def upgrade(engine, metadata):
    # Tables are created via db.create_all() in migrate(); this hook exists
    # for compatibility with versioned migration runners.
    migrate()


def downgrade(engine, metadata):
    try:
        from sqlalchemy import Table
        for name in ("sandbox_violations", "sandbox_executions", "agent_sandboxes"):
            tbl = Table(name, metadata, autoload_with=engine)
            tbl.drop(engine, checkfirst=True)
        print("Dropped Agent sandbox tables.")
        return True
    except Exception as e:
        print(f"Downgrade failed: {e}")
        return False


if __name__ == "__main__":
    from app import create_app
    app = create_app()
    with app.app_context():
        migrate()
