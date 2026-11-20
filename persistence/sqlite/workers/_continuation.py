"""Delegation recovery on the authorized worker's existing receipt stream."""

from persistence.core.errors import AuthorityRefusal
from session_rules.worker_continuation import is_terminal_outcome

from ..conversation._store.hydration import compaction_recovery_query, project_compaction_recovery
from ..conversation._store.worker_continuation import capture_terminal_on_connection, stamp_terminal_on_connection


def _worker_stamp(holder, conn, parent, payload):
    if set(payload) != {'status', 'exit_reason'} or not is_terminal_outcome(payload['status'], payload['exit_reason']):
        raise AuthorityRefusal('invalid_params')
    row = conn.execute("SELECT 1 FROM sessions WHERE id=? AND source='subagent' AND parent_session_id IS NOT NULL",
                       (parent,)).fetchone()
    return {'value': stamp_terminal_on_connection(holder, conn, parent, **payload) if row else None}


def _worker_capture(holder, conn, parent, payload):
    if (set(payload) != {'child', 'include_compacted'} or type(payload['include_compacted']) is not bool
            or not isinstance(payload['child'], str) or not payload['child'].strip()):
        raise AuthorityRefusal('invalid_params')
    result = capture_terminal_on_connection(holder, conn, payload['child'], parent,
                                            include_compacted=payload['include_compacted'], require_closed_owner=True)
    if not result.get('found'):
        return {'recovery': {'found': False}}
    return {'recovery': result}


def _worker_recover_compaction(holder, conn, session_id, payload):
    if payload:
        raise AuthorityRefusal('invalid_params')
    rows = conn.execute(compaction_recovery_query(), (session_id,)).fetchall()
    return {'recovery': project_compaction_recovery(holder, session_id, rows)}


WORKER_CONTINUATION_HANDLERS = {
    'continuation.stamp': _worker_stamp,
    'continuation.capture': _worker_capture,
    'continuation.recover_compaction': _worker_recover_compaction,
}
