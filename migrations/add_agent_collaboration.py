"""
Create Agent collaboration tables.
"""

import os
import re
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

from models import db  # noqa: E402


MYSQL_ENUM_COLUMNS = {
    "task_assignments": {
        "column": "state",
        "values": [
            "ASSIGNED",
            "CLAIMED",
            "RUNNING",
            "WAITING_HUMAN",
            "REVIEW",
            "DONE",
            "FAILED",
            "CANCELLED",
            "EXPIRED",
        ],
        "default": "ASSIGNED",
        "comment": "Assignment state",
    },
    "agent_runs": {
        "column": "status",
        "values": [
            "RUNNING",
            "WAITING_HUMAN",
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
            "EXPIRED",
        ],
        "default": "RUNNING",
        "comment": "Run status",
    },
}


def _quote_mysql_enum_value(value):
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _parse_mysql_enum_values(column_type):
    if not column_type or not column_type.lower().startswith("enum("):
        return []
    return [value.replace("''", "'") for value in re.findall(r"'((?:''|[^'])*)'", column_type)]


def _mysql_table_exists(connection, table_name):
    return connection.execute(db.text("SHOW TABLES LIKE :table_name"), {"table_name": table_name}).first() is not None


def _mysql_column_type(connection, table_name, column_name):
    row = connection.execute(db.text(f"SHOW COLUMNS FROM `{table_name}` LIKE :column_name"), {"column_name": column_name}).first()
    if not row:
        return None
    return row._mapping.get("Type")


def _ensure_mysql_agent_enum_values(connection):
    for table_name, spec in MYSQL_ENUM_COLUMNS.items():
        column_name = spec["column"]
        if not _mysql_table_exists(connection, table_name):
            continue

        column_type = _mysql_column_type(connection, table_name, column_name)
        existing_values = _parse_mysql_enum_values(column_type)
        if not existing_values:
            continue

        use_lowercase_values = all(value == value.lower() for value in existing_values)
        expected_values = [value.lower() for value in spec["values"]] if use_lowercase_values else spec["values"]
        missing_values = [value for value in expected_values if value not in existing_values]
        if not missing_values:
            continue

        updated_values = existing_values + missing_values
        enum_sql = ", ".join(_quote_mysql_enum_value(value) for value in updated_values)
        default_value = spec["default"].lower() if use_lowercase_values else spec["default"]
        if default_value not in updated_values:
            default_value = updated_values[0]
        connection.execute(db.text(
            f"ALTER TABLE `{table_name}` "
            f"MODIFY `{column_name}` ENUM({enum_sql}) "
            f"NOT NULL DEFAULT {_quote_mysql_enum_value(default_value)} "
            f"COMMENT {_quote_mysql_enum_value(spec['comment'])}"
        ))
        print(f"Added {', '.join(missing_values)} to {table_name}.{column_name} enum.")


def _postgres_type_exists(connection, type_name):
    return connection.execute(db.text("SELECT 1 FROM pg_type WHERE typname = :type_name"), {"type_name": type_name}).first() is not None


def _ensure_postgresql_agent_enum_values(connection):
    enum_specs = {
        "taskassignmentstate": "EXPIRED",
        "agentrunstatus": "EXPIRED",
    }
    for type_name, value in enum_specs.items():
        if not _postgres_type_exists(connection, type_name):
            continue
        connection.execute(db.text(f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{value}'"))
        print(f"Ensured {value} exists in PostgreSQL enum {type_name}.")


def ensure_agent_enum_values(connection):
    """Ensure existing native enum columns support the latest Agent terminal states."""
    dialect = connection.dialect.name
    if dialect == "mysql":
        _ensure_mysql_agent_enum_values(connection)
    elif dialect == "postgresql":
        _ensure_postgresql_agent_enum_values(connection)


def migrate():
    """Create missing Agent collaboration tables."""
    try:
        print("Creating Agent collaboration tables...")
        db.create_all()
        with db.engine.connect() as connection:
            ensure_agent_enum_values(connection)
            connection.commit()
        print("Agent collaboration tables created successfully.")
        return True
    except Exception as e:
        print(f"Migration failed: {e}")
        db.session.rollback()
        return False


if __name__ == "__main__":
    from app import create_app

    app = create_app()
    with app.app_context():
        migrate()
