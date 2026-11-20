"""Offline contracts for the session-switching diagnostic's database reader."""
import importlib.util
from pathlib import Path
import sqlite3

import pytest

_spec = importlib.util.spec_from_file_location(
    'reported_bugs_diagnostic', Path(__file__).parents[2] / 'scripts' / 'replicate_and_validate_reported_bugs.py'
)
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)


def test_latest_session_reads_existing_database_without_creating_one(tmp_path):
    db_path = tmp_path / 'state.db'
    with sqlite3.connect(db_path) as conn:
        conn.execute('CREATE TABLE sessions (id TEXT, title TEXT, last_activity_at INTEGER)')
        conn.executemany('INSERT INTO sessions VALUES (?, ?, ?)', [('a', 'first', 1), ('b', 'second', 2)])

    assert audit._latest_session(db_path) == ('b', 'second', 2)
    missing = tmp_path / 'missing.db'
    with pytest.raises(sqlite3.OperationalError):
        audit._latest_session(missing)
    assert not missing.exists()


def test_database_path_tracks_the_selected_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, 'get_hermes_home', lambda: tmp_path / 'profile')
    assert audit._state_db_path() == tmp_path / 'profile' / 'state.db'
