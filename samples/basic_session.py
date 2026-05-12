"""Basic central-session example.

Shows how to create one ``LlmingSession`` and attach typed app data to it.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

from llming_com import LlmingSession, LlmingSessionData, LlmingSessions, get_auth


@dataclass(frozen=True)
class ChatSetup:
    model: str
    language: str


@dataclass
class ChatSessionData(LlmingSessionData):
    """Chat-specific state attached to the central session."""

    model: str = "gpt-4o"
    language: str = "en"

    @classmethod
    async def create(cls, session: LlmingSession, context=None) -> "ChatSessionData":
        if isinstance(context, ChatSetup):
            return cls(model=context.model, language=context.language)
        return cls()


async def main():
    os.environ.setdefault("LLMING_AUTH_SECRET", "demo")

    registry = LlmingSessions.registry()
    registry.reset()

    auth = get_auth()
    sid1, token1 = auth.make_auth_cookie_value()
    sid2, token2 = auth.make_auth_cookie_value()

    alice = registry.register(sid1, LlmingSession(user_id="alice"))
    bob = registry.register(sid2, LlmingSession(user_id="bob"))

    with alice.active():
        await ChatSessionData.current(context=ChatSetup("claude-sonnet", "de"))
    with bob.active():
        await ChatSessionData.current(context=ChatSetup("gpt-4o", "en"))

    print(f"Active sessions: {registry.active_count}")

    session = registry.get_session(sid1)
    if session is not None:
        data = session.optional_data(ChatSessionData)
        if data is not None:
            print(
                f"Session {sid1[:8]}...: "
                f"user={session.user_id}, model={data.model}, lang={data.language}"
            )

    for sid, session in registry.list_sessions().items():
        data = session.optional_data(ChatSessionData)
        if data is not None:
            print(f"  {sid[:8]}... -> {session.user_id} ({data.model})")

    registry.remove(sid2)
    print(f"After removal: {registry.active_count} active")

    _ = token1, token2


if __name__ == "__main__":
    asyncio.run(main())
