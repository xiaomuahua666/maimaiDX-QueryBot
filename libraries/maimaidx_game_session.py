"""Per-group game admission gate.

NoneBot can dispatch two matching messages concurrently.  A plain
``is_busy`` check is therefore insufficient when a handler awaits network or
render work before registering its game state.  This small gate reserves a
group before the first await and makes all game modes share the same
reservation.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Dict, Hashable


class GroupGameGate:
    """Serialize game admission per group while keeping reservations explicit."""

    def __init__(self) -> None:
        self._locks: Dict[Hashable, asyncio.Lock] = {}
        self._reserved: Dict[Hashable, str] = {}

    def _lock(self, gid: Hashable) -> asyncio.Lock:
        lock = self._locks.get(gid)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[gid] = lock
        return lock

    async def acquire(
        self,
        gid: Hashable,
        *,
        mode: str,
        busy_check: Callable[[Hashable], bool],
    ) -> bool:
        """Reserve ``gid`` if no game is active or being prepared."""
        async with self._lock(gid):
            if gid in self._reserved or busy_check(gid):
                return False
            self._reserved[gid] = mode
            return True

    def release(self, gid: Hashable) -> None:
        self._reserved.pop(gid, None)

    def is_reserved(self, gid: Hashable) -> bool:
        return gid in self._reserved

    def mode(self, gid: Hashable) -> str | None:
        return self._reserved.get(gid)


game_session_gate = GroupGameGate()

