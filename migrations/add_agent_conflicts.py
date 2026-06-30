"""Add agent_conflicts table for Agent collaboration conflict detection
and resolution (Increment 89).

Revision: add_agent_conflicts
"""

from models import db


def migrate():
    """Create missing conflict table via SQLAlchemy metadata."""
    try:
        print("Creating Agent conflict table...")
        db.create_all()
        print("Agent conflict table created successfully.")
        return True
    except Exception as e:
        print(f"Migration failed: {e}")
        db.session.rollback()
        return False


def upgrade(engine, metadata):
    migrate()


def downgrade(engine, metadata):
    try:
        from sqlalchemy import Table
        tbl = Table("agent_conflicts", metadata, autoload_with=engine)
        tbl.drop(engine, checkfirst=True)
        print("Dropped agent_conflicts table.")
        return True
    except Exception as e:
        print(f"Downgrade failed: {e}")
        return False


if __name__ == "__main__":
    from app import create_app
    app = create_app()
    with app.app_context():
        migrate()
