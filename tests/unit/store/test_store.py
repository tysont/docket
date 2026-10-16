from __future__ import annotations

import sqlite3

import pytest

from docket.cli.errors import DocketError
from docket.store.blobs import BlobStore
from docket.store.db import MIGRATIONS, SCHEMA_VERSION, open_db
from docket.store.paths import state_root, workspace_slug

pytestmark = pytest.mark.smoke
TABLES = {"runs", "cursors", "cursor_proposals", "units", "processed_ids", "dispositions", "receipts",
          "unexpected", "ledgers", "leases", "schema_meta"}


def test_migration_creates_tables_and_is_idempotent(tmp_path):
    db = open_db(tmp_path / "docket.db")
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert TABLES <= names
    assert db.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0] == str(SCHEMA_VERSION)
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    db.close()
    db = open_db(tmp_path / "docket.db")
    # migration numbers are reserved per milestone, so the sequence may have gaps
    assert db.execute("SELECT COUNT(*) FROM schema_meta WHERE key LIKE 'migration.%'").fetchone()[0] == len(MIGRATIONS)
    db.close()


def test_refuses_newer_database(tmp_path):
    db = open_db(tmp_path / "docket.db")
    db.execute("UPDATE schema_meta SET value=? WHERE key='version'", (str(SCHEMA_VERSION + 5),))
    db.commit()
    db.close()
    with pytest.raises(DocketError) as ei:
        open_db(tmp_path / "docket.db")
    assert ei.value.code == "store.db_too_new" and ei.value.exit_code == 7
    assert ei.value.details["db_version"] == SCHEMA_VERSION + 5


def test_cli_reports_db_too_new(cli, persona, pinned_now):
    ws = persona("support-lead")
    code, env = cli("run", "start", "shift", "--now", pinned_now, "--workspace", str(ws))
    import os
    root = state_root(ws, os.environ)
    db = sqlite3.connect(root / "docket.db")
    db.execute("UPDATE schema_meta SET value='99' WHERE key='version'")
    db.commit()
    db.close()
    code, env = cli("next", "--workspace", str(ws))
    assert code == 7 and env["error"]["code"] == "store.db_too_new"


def test_blob_store_is_content_addressed(tmp_path):
    b = BlobStore(tmp_path / "blobs")
    ref = b.put(b"hello")
    assert ref.startswith("sha256:") and b.put(b"hello") == ref and b.get(ref) == b"hello"
    hexd = ref.split(":")[1]
    assert (tmp_path / "blobs" / hexd[:2] / hexd).is_file()


def test_state_root_from_env(tmp_path):
    env = {"HOME": str(tmp_path / "h")}
    ws = tmp_path / "My Workspace"
    assert state_root(ws, env).parent == tmp_path / "h" / ".local" / "state" / "docket"
    assert workspace_slug(ws).startswith("my-workspace-")
    env["DOCKET_STATE_ROOT"] = str(tmp_path / "s")
    assert state_root(ws, env).parent == tmp_path / "s"
