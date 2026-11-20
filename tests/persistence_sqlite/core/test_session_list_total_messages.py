"""The sidebar count covers durable history, including compaction archives."""

from persistence.conversation.messages import NewMessage
from persistence.conversation.sessions import SessionQuery
from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_seed import seed_messages, seed_session


def test_list_sessions_rich_total_message_count_preservation(tmp_path):
    with open_persistence(profile_scope(home=tmp_path)) as store:
        seed_session(store, "root", "cli")
        seed_messages(store, "root", {"role": "user", "content": "one"},
                      {"role": "assistant", "content": "two"}, {"role": "user", "content": "three"})
        seed_session(store, "tip", "cli", parent_session_id="root")
        store.sessions.end("root", "compression")
        seed_messages(store, "tip", {"role": "user", "content": "four"},
                      {"role": "assistant", "content": "five"})

        (entry,) = store.sessions.list_sessions(SessionQuery())
        row = entry.as_list_row()
        assert row["id"] == "tip"
        assert row["message_count"] == 2
        assert row["total_message_count"] == 5


def test_list_sessions_rich_in_place_compaction_total_count(tmp_path):
    with open_persistence(profile_scope(home=tmp_path)) as store:
        seed_session(store, "chat", "cli")
        seed_messages(store, "chat", *({"role": ("user", "assistant")[i % 2], "content": f"Turn {i}"}
                                    for i in range(6)))
        store.messages.compact("chat", [NewMessage(role="system", content="Summary"),
                                        NewMessage(role="assistant", content="Turn 5")])
        seed_messages(store, "chat", {"role": "user", "content": "Follow-up"})

        (entry,) = store.sessions.list_sessions(SessionQuery())
        row = entry.as_list_row()
        assert row["id"] == "chat"
        assert row["message_count"] == 3
        assert row["total_message_count"] == 9
