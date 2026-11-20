"""Resolve the transcript repository of the agent's already attached store."""

from agent.agent_init_session import agent_interface, stand_in_interface
from persistence_assembly import Persistence


def attached_message_repository(agent, db):
    if isinstance(db, Persistence):
        return db.messages
    if (store := agent_interface(agent) or stand_in_interface(db)) is not None:
        return store.messages
    # Keep reads on this client; never open another profile's store as a fallback.
    return db


def attached_store_key(agent, db):
    """Return the profile key without opening the attached store."""
    store = db if isinstance(db, Persistence) else agent_interface(agent) or stand_in_interface(db)
    if isinstance(store, Persistence):
        return store.scope.hermes_home_key
    return None
