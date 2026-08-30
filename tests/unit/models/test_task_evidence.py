"""Tests for Task DoD fields and TaskEvidenceRecord model."""

import pytest


class TestTaskDodFields:
    """Task.dod 与 human_intervention_count 字段。"""

    def test_task_defaults(self, db_session, project_factory, task_factory):
        project = project_factory()
        task = task_factory(project_id=project.id, title="DoD defaults")

        assert task.dod is None
        assert (task.human_intervention_count or 0) == 0
        assert task.to_dict()["dod"] == []
        assert task.to_dict()["human_intervention_count"] == 0

    def test_task_dod_roundtrip(self, db_session, project_factory, task_factory):
        project = project_factory()
        dod = [
            {"type": "test", "value": "pytest tests/ -x"},
            {"type": "build", "value": "npm run build"},
        ]
        task = task_factory(project_id=project.id, title="DoD roundtrip", dod=dod)
        db_session.add(task)
        db_session.commit()

        reloaded = type(task).query.get(task.id)
        assert reloaded.dod == dod
        assert reloaded.to_dict()["dod"] == dod

    def test_human_intervention_increment(self, db_session, project_factory, task_factory):
        project = project_factory()
        task = task_factory(project_id=project.id, title="ACR counter")
        task.human_intervention_count = (task.human_intervention_count or 0) + 1
        db_session.commit()

        assert type(task).query.get(task.id).human_intervention_count == 1


class TestTaskEvidenceRecord:
    """TaskEvidenceRecord 模型。"""

    def test_create_evidence(self, db_session, project_factory, task_factory):
        from models import TaskEvidenceRecord

        project = project_factory()
        task = task_factory(project_id=project.id, title="Evidence target")

        evidence = TaskEvidenceRecord(
            task_id=task.id,
            attempt_id="att_test123",
            evidence_type="test",
            status="passed",
            summary="24 passed, 0 failed",
            detail={"command": "pytest -q", "exit_code": 0},
            created_by="agent:1",
        )
        db_session.add(evidence)
        db_session.commit()

        assert evidence.id is not None
        stored = TaskEvidenceRecord.query.filter_by(task_id=task.id).first()
        assert stored.evidence_type == "test"
        assert stored.status == "passed"
        assert stored.detail["exit_code"] == 0

    def test_evidence_types_and_statuses_contract(self):
        from models.task_evidence import TaskEvidence

        assert "test" in TaskEvidence.TYPES
        assert "build" in TaskEvidence.TYPES
        assert "lint" in TaskEvidence.TYPES
        assert "command" in TaskEvidence.TYPES
        assert "pr" in TaskEvidence.TYPES
        assert {"passed", "failed", "unknown"} == set(TaskEvidence.STATUSES)

    def test_evidence_repr(self, db_session, project_factory, task_factory):
        from models import TaskEvidenceRecord

        project = project_factory()
        task = task_factory(project_id=project.id, title="Evidence repr")
        evidence = TaskEvidenceRecord(
            task_id=task.id, evidence_type="build", status="failed"
        )
        db_session.add(evidence)
        db_session.commit()

        assert "build" in repr(evidence)
        assert "failed" in repr(evidence)
