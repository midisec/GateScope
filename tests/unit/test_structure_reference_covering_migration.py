from contextlib import contextmanager
from importlib import import_module
from types import SimpleNamespace

import pytest


migration = import_module("migrations.versions.0039_structure_reference_covering")


class Operations:
    def __init__(self, states, dialect="postgresql"):
        self.states = iter(states)
        self.dialect = SimpleNamespace(name=dialect)
        self.statements = []
        self.autocommit = False
        self.blocks = 0

    def get_bind(self):
        return self

    def get_context(self):
        return self

    @contextmanager
    def autocommit_block(self):
        self.blocks += 1
        self.autocommit = True
        try:
            yield
        finally:
            self.autocommit = False

    def exec_driver_sql(self, statement):
        assert self.autocommit
        assert "n.nspname='public'" in statement
        assert "c.relname='structure_reference_covering'" in statement
        return SimpleNamespace(fetchone=lambda: next(self.states))

    def execute(self, statement):
        assert self.autocommit
        self.statements.append(statement)


def state(*, valid=True, ready=True, live=True, definition=None, same_table=True, kind="i"):
    return (kind, same_table, definition or migration.INDEX_DEFINITION, valid, ready, live)


def test_new_covering_index_is_concurrent_and_keeps_legacy_index_and_permissions(monkeypatch):
    operations = Operations([None, state()])
    monkeypatch.setattr(migration, "op", operations)
    migration.upgrade()
    assert operations.statements == [migration.CREATE_INDEX]
    assert operations.blocks == 1
    assert "(context_id, pattern, entry_phase, score_bucket, stage_time)" in migration.CREATE_INDEX
    assert "INCLUDE (candidate_id, phase, resolved_time, sampled, formed)" in migration.CREATE_INDEX
    assert not any("GRANT" in sql or "structure_reference_lookup" in sql for sql in operations.statements)


@pytest.mark.parametrize("flags", [{"valid": False}, {"ready": False}, {"live": False}])
def test_invalid_leftover_is_dropped_exactly_once_then_rebuilt(monkeypatch, flags):
    operations = Operations([state(**flags), state()])
    monkeypatch.setattr(migration, "op", operations)
    migration.upgrade()
    assert operations.statements == [migration.DROP_INDEX, migration.CREATE_INDEX]


def test_valid_index_is_idempotent_after_interrupted_alembic_version_write(monkeypatch):
    operations = Operations([state()])
    monkeypatch.setattr(migration, "op", operations)
    migration.upgrade()
    assert operations.statements == []


@pytest.mark.parametrize("changes", [
    {"definition": "CREATE INDEX structure_reference_covering ON unrelated (id)"},
    {"same_table": False}, {"kind": "r"},
])
@pytest.mark.parametrize("action", ["upgrade", "downgrade"])
def test_name_collision_never_drops_a_different_object(monkeypatch, changes, action):
    operations = Operations([state(valid=False, **changes)])
    monkeypatch.setattr(migration, "op", operations)
    with pytest.raises(RuntimeError, match="Unexpected object"):
        getattr(migration, action)()
    assert operations.statements == []


def test_downgrade_drops_only_exact_new_index_concurrently(monkeypatch):
    operations = Operations([state()])
    monkeypatch.setattr(migration, "op", operations)
    migration.downgrade()
    assert operations.statements == ["DROP INDEX CONCURRENTLY IF EXISTS public.structure_reference_covering"]


@pytest.mark.parametrize("action", ["upgrade", "downgrade"])
def test_sqlite_is_noop(monkeypatch, action):
    operations = Operations([], dialect="sqlite")
    monkeypatch.setattr(migration, "op", operations)
    getattr(migration, action)()
    assert operations.blocks == 0 and operations.statements == []


def test_failed_post_build_validation_does_not_claim_success(monkeypatch):
    operations = Operations([None, state(valid=False)])
    monkeypatch.setattr(migration, "op", operations)
    with pytest.raises(RuntimeError, match="did not become valid"):
        migration.upgrade()
