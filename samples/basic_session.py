"""Basic session management example.

Shows how to create a custom session entry, register sessions,
and look them up by ID.
"""

from dataclasses import dataclass

from llming_com import BaseSessionEntry, BaseSessionRegistry, make_auth_cookie_value


@dataclass
class ChatSession(BaseSessionEntry):
    """Session entry with a model field."""
    model: str = "gpt-4o"
    language: str = "en"


class ChatRegistry(BaseSessionRegistry["ChatSession"]):
    """Application-level session registry."""

    def on_session_expired(self, session_id, entry):
        print(f"Session {session_id} for user {entry.user_id} expired")


def main():
    registry = ChatRegistry.get()

    # Create two sessions
    sid1, token1 = make_auth_cookie_value()
    sid2, token2 = make_auth_cookie_value()

    registry.register(sid1, ChatSession(user_id="alice", model="claude-sonnet", language="de"))
    registry.register(sid2, ChatSession(user_id="bob", model="gpt-4o", language="en"))

    print(f"Active sessions: {registry.active_count}")

    # Look up a session
    entry = registry.get_session(sid1)
    print(f"Session {sid1[:8]}...: user={entry.user_id}, model={entry.model}, lang={entry.language}")

    # List all sessions
    for sid, e in registry.list_sessions().items():
        print(f"  {sid[:8]}... -> {e.user_id} ({e.model})")

    # Remove a session
    registry.remove(sid2)
    print(f"After removal: {registry.active_count} active")


if __name__ == "__main__":
    main()
