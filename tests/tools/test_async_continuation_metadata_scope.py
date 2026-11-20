"""A failed task cannot borrow continuation metadata from its enclosing event."""
import pytest

from tools.process_registry_notifications import format_process_notification


@pytest.mark.parametrize('metadata', [{}, {'child_session_id': ''}, {'child_session_id': None}])
def test_failure_result_does_not_borrow_top_level_identity(metadata):
    event = {
        'type': 'async_delegation', 'delegation_id': 'scope-test',
        'task_failure_notice': True, 'goals': ['one', 'two'],
        'child_session_id': 'unrelated-top-level-worker', 'continuation_available': True,
        'results': [{'task_index': 1, 'status': 'error', 'error': 'failed', **metadata}],
    }
    notice = format_process_notification(event)
    assert 'Child session ID:' not in notice
    assert 'Continuation available:' not in notice


def test_legacy_flat_failure_can_use_its_own_metadata():
    event = {'type': 'async_delegation', 'task_failure_notice': True,
             'child_session_id': 'flat-worker', 'continuation_available': False}
    notice = format_process_notification(event)
    assert 'Child session ID: flat-worker' in notice
    assert 'Continuation available: false' in notice
