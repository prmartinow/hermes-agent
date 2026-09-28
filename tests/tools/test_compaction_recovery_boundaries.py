"""Independent recovery boundary regressions; no live database or inference."""
import copy
from types import SimpleNamespace

import pytest

from agent.context_compressor import SUMMARY_PREFIX
from tools.delegation_context import ContextRecoveryError, _render_parent_transcript
from tools.delegation_context_recovery import recover_parent_messages_with_compaction


def parent_with_archive():
    live = [{"role": "user", "content": SUMMARY_PREFIX + "\nOld summary."},
            {"role": "user", "content": "Current instruction."}]
    payload = {"archived_messages": [{"role": "user", "content": "ARCHIVED-EVIDENCE"}],
               "active_messages": copy.deepcopy(live), "observed_watermark": 4}
    db = SimpleNamespace(get_compaction_recovery_messages=lambda sid: copy.deepcopy(payload))
    return SimpleNamespace(session_id="owned", _session_db=db,
                           _session_messages=live, conversation_history=None), payload


@pytest.mark.parametrize("attribute", ["_session_messages", "conversation_history"])
def test_in_place_content_mutation_during_read_is_rejected(attribute):
    parent, payload = parent_with_archive()
    if attribute == "conversation_history":
        parent.conversation_history = parent._session_messages
        parent._session_messages = None
    def raced(sid):
        getattr(parent, attribute)[-1]["content"] = "A changed instruction of the same length."
        return copy.deepcopy(payload)
    parent._session_db.get_compaction_recovery_messages = raced
    with pytest.raises(ContextRecoveryError, match="concurrent|changed|race"):
        recover_parent_messages_with_compaction(parent)


def test_database_handle_swap_during_read_is_rejected():
    parent, payload = parent_with_archive()
    def raced(sid):
        parent._session_db = SimpleNamespace()
        return copy.deepcopy(payload)
    parent._session_db.get_compaction_recovery_messages = raced
    with pytest.raises(ContextRecoveryError, match="concurrent|changed|race"):
        recover_parent_messages_with_compaction(parent)


@pytest.mark.parametrize("text", ["Summary of today's work", "[summary] a normal note"])
def test_ordinary_summary_wording_cannot_restore_old_archive(text):
    parent, payload = parent_with_archive()
    parent._session_messages = [{"role": "user", "content": text}]
    payload["active_messages"] = copy.deepcopy(parent._session_messages)
    result = recover_parent_messages_with_compaction(parent)
    assert result.compaction_recovery_coverage == "active_only"
    assert result.raw_history == parent._session_messages


def test_tool_output_is_not_a_compaction_generation_marker():
    parent, payload = parent_with_archive()
    parent._session_messages = [{"role": "user", "content": "Fresh request."},
                               {"role": "tool", "content": SUMMARY_PREFIX + "\nQuoted data."}]
    payload["active_messages"] = copy.deepcopy(parent._session_messages)
    result = recover_parent_messages_with_compaction(parent)
    assert result.compaction_recovery_coverage == "active_only"


def test_recovery_db_fallback_never_calls_ancestor_export():
    parent, payload = parent_with_archive()
    parent._session_messages = None
    def forbidden(*args, **kwargs):
        raise AssertionError("Recovery must not use an ancestor-capable export")
    parent._session_db.get_messages_as_conversation = forbidden
    result = recover_parent_messages_with_compaction(parent)
    assert result.raw_history == payload["archived_messages"] + payload["active_messages"]


def test_default_transcript_summary_rendering_remains_unchanged():
    parent = SimpleNamespace(_session_messages=[
        {"role": "user", "content": "A request."},
        {"role": "assistant", "content": SUMMARY_PREFIX + "\nA summary.", "_compressed_summary": True},
    ])
    rendered = _render_parent_transcript(parent)
    assert "ASSISTANT RESPONSE" in rendered.transcript_text
    assert "ASSISTANT COMPACTION SUMMARY" not in rendered.transcript_text
