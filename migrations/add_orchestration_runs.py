"""Add agent_orchestration_runs table for orchestration cycle history (Increment 99).

Revision: add_orchestration_runs
"""

from models import db


def migrate():
    """Create the orchestration runs history table via SQLAlchemy metadata."""
    try:
        print("Creating OrchestrationRun table...")
        db.create_all()
        print("OrchestrationRun table created successfully.")
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
        tbl = Table("agent_orchestration_runs", metadata, autoload_with=engine)
        tbl.drop(engine, checkfirst=True)
        print("OrchestrationRun table dropped.")
    except Exception as e:
        print(f"Downgrade failed: {e}")
