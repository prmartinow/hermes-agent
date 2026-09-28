"""Independent continuation source-gate regressions with private SQLite state."""
import contextlib
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_state import SessionDB
from tools.delegation_context import ContextInheritanceError
from tools.delegation_context_continuation import stamp_child_terminal_state, validate_and_capture_prior_worker


@pytest.fixture
def source(tmp_path):
    db = SessionDB(tmp_path / 'source.db')
    db.create_session('parent', source='test')
    db.create_session('worker', source='subagent', parent_session_id='parent',
                      model_config={'_delegate_from': 'parent'})
    db.append_messages_batch('worker', [{'role': 'user', 'content': 'Known worker evidence.'}])
    child = SimpleNamespace(session_id='worker', _session_db=db, _session_init_model_config={})
    assert stamp_child_terminal_state(child, {'status': 'completed', 'exit_reason': 'completed'})
    db.end_session('worker', 'agent_close')
    parent = SimpleNamespace(session_id='parent', _session_db=db, _active_children=[])
    try:
        yield parent, db
    finally:
        db.close()


def test_stamped_but_not_closed_source_is_rejected(source):
    parent, db = source
    db._write_sql('UPDATE sessions SET ended_at=NULL WHERE id=?', ('worker',))
    with pytest.raises(ContextInheritanceError):
        validate_and_capture_prior_worker(parent, 'worker')


@pytest.mark.parametrize('field,value', [
    ('version', True), ('version', 1.0), ('message_count', True),
    ('max_row_id', True), ('status', 'running'), ('exit_reason', ''),
    ('completed_at', 'not-a-time'), ('exit_reason', 'unknown-outcome'),
    ('status', 'interrupted'),
])
def test_malformed_terminal_values_are_not_coerced(source, field, value):
    parent, db = source
    cfg = json.loads(db._read_one('SELECT model_config FROM sessions WHERE id=?', ('worker',))[0])
    cfg['_delegate_terminal'][field] = value
    db.patch_session_model_config('worker', {'_delegate_terminal': cfg['_delegate_terminal']})
    with pytest.raises(ContextInheritanceError):
        validate_and_capture_prior_worker(parent, 'worker')


def test_same_count_and_row_id_content_edit_invalidates_terminal_source(source):
    parent, db = source
    db._write_sql('UPDATE messages SET content=? WHERE session_id=?', ('Rewritten evidence.', 'worker'))
    with pytest.raises(ContextInheritanceError):
        validate_and_capture_prior_worker(parent, 'worker')


def test_parent_identity_change_during_capture_is_rejected(source):
    parent, db = source
    capture = db.capture_terminal_child_session
    def changed(*args, **kwargs):
        result = capture(*args, **kwargs)
        parent.session_id = 'changed-parent'
        return result
    with patch.object(db, 'capture_terminal_child_session', changed):
        with pytest.raises(ContextInheritanceError):
            validate_and_capture_prior_worker(parent, 'worker')


def test_malformed_lease_does_not_mean_inactive(source):
    parent, db = source
    capture = db.capture_terminal_child_session
    def malformed(*args, **kwargs):
        result = capture(*args, **kwargs)
        result['lease'] = {'holder': 'unknown', 'expires_at': 'invalid'}
        return result
    with patch.object(db, 'capture_terminal_child_session', malformed):
        with pytest.raises(ContextInheritanceError):
            validate_and_capture_prior_worker(parent, 'worker')


def test_other_database_active_worker_with_same_ids_is_not_local(source, tmp_path):
    from tools.delegate_tool_registry import _active_subagents
    parent, _ = source
    other = SessionDB(tmp_path / 'other.db')
    other_agent = SimpleNamespace(session_id='worker', _session_db=other)
    try:
        with patch.dict(_active_subagents, {'other': {'agent': other_agent, 'owner_agent_session_id': 'parent'}}, clear=True):
            captured = validate_and_capture_prior_worker(parent, 'worker')
        assert captured.session_id == 'worker'
    finally:
        other.close()


def test_failed_begin_is_not_silently_accepted_as_atomic(source):
    _, db = source
    read_ctx = db._read_ctx
    attempted_failure = []
    @contextlib.contextmanager
    def failing_begin():
        with read_ctx() as conn:
            class Proxy:
                def execute(self, sql, *args):
                    if sql.strip().upper() == 'BEGIN':
                        attempted_failure.append(True)
                        raise sqlite3.OperationalError('synthetic BEGIN failure')
                    return conn.execute(sql, *args)
                def __getattr__(self, name):
                    return getattr(conn, name)
            yield Proxy()
    with patch.object(db, '_read_ctx', failing_begin):
        try:
            db.capture_terminal_child_session('worker', 'parent')
        except (sqlite3.Error, ContextInheritanceError):
            return
    assert not attempted_failure, 'A failed BEGIN was swallowed and capture continued'


def test_capture_is_one_statement_and_does_not_decode_foreign_messages(source):
    _, db = source
    original = db._read_ctx
    statements = []
    @contextlib.contextmanager
    def observed():
        with original() as conn:
            class Proxy:
                def execute(self, sql, *args):
                    statements.append(sql)
                    return conn.execute(sql, *args)
                def __getattr__(self, name):
                    return getattr(conn, name)
            yield Proxy()
    with patch.object(db, '_read_ctx', observed):
        captured = db.capture_terminal_child_session('worker', 'parent')
    assert len(statements) == 1 and statements[0].startswith('SELECT ')
    assert captured['active_count'] == 1
    foreign = db.capture_terminal_child_session('worker', 'another-parent')
    assert foreign['active_messages'] == []
    assert foreign['archived_messages'] == []


def test_availability_uses_the_same_atomic_eligibility_gate(source):
    from tools.delegation_context_continuation import verify_child_terminal_readback
    parent, db = source
    with patch.object(db, 'capture_terminal_child_session', wraps=db.capture_terminal_child_session) as capture:
        assert verify_child_terminal_readback(db, 'worker', parent.session_id)
        capture.assert_called_once()


@pytest.mark.parametrize('batch', [False, True])
def test_parent_change_between_render_and_prior_capture_rejects_whole_build(source, batch):
    import tools.delegation_context as context
    parent, _ = source
    parent._session_messages = [{"role": "user", "content": "Original instruction."}]
    original = context._render_parent_transcript
    def changed(*args, **kwargs):
        rendered = original(*args, **kwargs)
        parent._session_messages[0]["content"] = "A newer instruction."
        return rendered
    with patch.object(context, '_render_parent_transcript', changed):
        with pytest.raises(ContextInheritanceError, match="changed"):
            if batch:
                context.build_batch_context_snapshots(
                    parent, [True], [5000], task_continue_from=['worker'])
            else:
                context.build_delegation_context_snapshot(
                    parent, continue_from='worker', config_override_tokens=5000)


