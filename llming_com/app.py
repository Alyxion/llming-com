"""Typed application context for llming-com."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from llming_com.session import BaseSessionEntry, BaseSessionRegistry

E = TypeVar("E", bound=BaseSessionEntry)


@dataclass
class BaseLlmingApp(Generic[E]):
    """Application-wide context injected into ``AppRouter`` handlers."""

    registry: BaseSessionRegistry[E]

    async def call(self, session_id: str, target: str, *args, **kwargs) -> bool:
        """Call a client method on one session."""
        session = self.registry.get_session(session_id)
        if session is None:
            return False
        return await session.call(target, *args, **kwargs)

    async def broadcast(self, target: str, *args, **kwargs) -> int:
        """Call a client method on every active session."""
        sent = 0
        for session in self.registry.list_sessions().values():
            if await session.call(target, *args, **kwargs):
                sent += 1
        return sent
