from contextlib import contextmanager
from copy import deepcopy
from importlib import import_module
from types import SimpleNamespace

import pytest

migration = import_module("migrations.versions.0055_drop_redundant_stage_lookup")


def state(*, covering=False, **changes):
    result = dict(oid=2 if covering else 1, kind="i", same_table=True,
                  definition=migration.COVER_DEFINITION if covering else migration.OLD_DEFINITION,
                  valid=True, ready=True, live=True, unique=False, primary=False, exclusion=False,
                  replica_identity=False, clustered=False, key_attributes=[1, 3, 4, 5, 6],
                  opclasses=[100, 100, 100, 100, 101], collations=[100, 100, 100, 100, 0],
                  options=[0, 0, 0, 0, 0], key_count=5, attribute_count=10 if covering else 5,
                  predicate=None, expressions=None, constraint_dependency=False,
                  incoming_dependency=False, extension_dependency=False)
    result.update(changes)
    return result


class Operations:
    def __init__(self, *, old="DEFAULT", cover="DEFAULT", dialect="postgresql", cancel=False):
        self.old = state() if old == "DEFAULT" else deepcopy(old)
        self.cover = state(covering=True) if cover == "DEFAULT" else deepcopy(cover)
        self.dialect = SimpleNamespace(name=dialect)
        self.statements = []
        self.autocommit = False
        self.cancel = cancel
        self.restored = False

    def get_bind(self):
        return self

    def get_context(self):
        return self

    @contextmanager
    def autocommit_block(self):
        self.autocommit = True
        try:
            yield
        finally:
            self.autocommit = False

    def exec_driver_sql(self, sql, params=None):
        assert self.autocommit
        if sql.startswith("SELECT current_setting"):
            value = ("7s", "9s")
        elif sql.startswith("SELECT set_config"):
            assert params == ("7s", "9s")
            self.restored = True
            value = ("7s", "9s")
        else:
            assert "pg_depend" in sql and "pg_constraint" in sql and "indisreplident" in sql
            data = self.old if params == (migration.OLD_NAME,) else self.cover
            value = (data,) if data is not None else None
        return SimpleNamespace(fetchone=lambda: value)

    def execute(self, sql):
        assert self.autocommit
        self.statements.append(sql)
        if sql == migration.DROP_INDEX:
            if self.cancel:
                self.old["valid"] = False
                raise RuntimeError("statement timeout")
            self.old = None
        elif sql == migration.CREATE_INDEX:
            self.old = state()


def ddl(operations):
    return [sql for sql in operations.statements if not sql.startswith("SET ")]


def test_drop_is_concurrent_bounded_and_never_rebuilds_or_changes_data(monkeypatch):
    operations = Operations()
    original_cover = deepcopy(operations.cover)
    monkeypatch.setattr(migration, "op", operations)
    migration.upgrade()
    assert ddl(operations) == [migration.DROP_INDEX]
    assert operations.cover == original_cover and operations.old is None
    assert "SET lock_timeout='1s'" in operations.statements
    assert "SET statement_timeout='30s'" in operations.statements
    assert operations.restored


@pytest.mark.parametrize("changes", [
    {"valid": False}, {"ready": False}, {"live": False}, {"kind": "r"},
    {"same_table": False}, {"definition": "CREATE INDEX unrelated ON another (x)"},
    {"unique": True}, {"primary": True}, {"replica_identity": True}, {"clustered": True},
    {"constraint_dependency": True}, {"incoming_dependency": True}, {"extension_dependency": True},
    {"predicate": "sampled"}, {"key_count": 4},
])
def test_bad_or_unusable_cover_never_removes_anything(monkeypatch, changes):
    operations = Operations(cover=state(covering=True, **changes))
    monkeypatch.setattr(migration, "op", operations)
    with pytest.raises(RuntimeError):
        migration.upgrade()
    assert ddl(operations) == [] and operations.restored


@pytest.mark.parametrize("key", ["key_attributes", "opclasses", "collations", "options"])
def test_exact_search_semantics_must_match_not_just_text_definition(monkeypatch, key):
    old = state()
    old[key][0] += 1
    operations = Operations(old=old)
    monkeypatch.setattr(migration, "op", operations)
    with pytest.raises(RuntimeError, match="KEYS_NOT_IDENTICAL"):
        migration.upgrade()
    assert ddl(operations) == []


@pytest.mark.parametrize("changes", [{"primary": True}, {"replica_identity": True},
                                    {"constraint_dependency": True}, {"incoming_dependency": True},
                                    {"definition": "CREATE INDEX structure_reference_lookup ON unrelated (id)"}])
def test_old_index_identity_and_dependencies_are_protected(monkeypatch, changes):
    operations = Operations(old=state(**changes))
    monkeypatch.setattr(migration, "op", operations)
    with pytest.raises(RuntimeError):
        migration.upgrade()
    assert ddl(operations) == []


def test_completed_drop_and_interrupted_version_update_is_idempotent(monkeypatch):
    operations = Operations(old=None)
    monkeypatch.setattr(migration, "op", operations)
    migration.upgrade()
    assert ddl(operations) == [] and operations.restored


def test_cancelled_drop_is_diagnosed_and_guarded_retry_finishes(monkeypatch):
    operations = Operations(cancel=True)
    monkeypatch.setattr(migration, "op", operations)
    with pytest.raises(RuntimeError, match="INTERRUPTED_VALID_READY_LIVE=False:True:True"):
        migration.upgrade()
    assert operations.restored and operations.old["valid"] is False
    operations.cancel = False
    migration.upgrade()
    assert operations.old is None and operations.cover["valid"] is True


def test_invalid_legacy_retry_still_refuses_unusable_cover(monkeypatch):
    operations = Operations(old=state(valid=False), cover=state(covering=True, valid=False))
    monkeypatch.setattr(migration, "op", operations)
    with pytest.raises(RuntimeError, match="COVERING_STAGE_INDEX_NOT_USABLE"):
        migration.upgrade()
    assert ddl(operations) == []


def test_downgrade_explicitly_rebuilds_legacy_not_cover(monkeypatch):
    operations = Operations(old=None)
    monkeypatch.setattr(migration, "op", operations)
    migration.downgrade()
    assert ddl(operations) == [migration.CREATE_INDEX]
    assert operations.restored and operations.old["valid"]


@pytest.mark.parametrize("action", ["upgrade", "downgrade"])
def test_sqlite_noop(monkeypatch, action):
    operations = Operations(dialect="sqlite")
    monkeypatch.setattr(migration, "op", operations)
    getattr(migration, action)()
    assert operations.statements == []
