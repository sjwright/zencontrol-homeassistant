"""Rate limiter for batched async operations (from mqtt_bridge)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Coroutine
from typing import Any


class RateLimiter:
    """Limit concurrent coroutine execution with a minimum delay between batches."""

    def __init__(
        self, max_concurrent: int = 5, delay_between_batches: float = 0.1
    ) -> None:
        self._max_concurrent = max_concurrent
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.delay_between_batches = delay_between_batches
        self.last_batch_time = 0.0

    async def execute[T](self, coro: Coroutine[Any, Any, T]) -> T:
        """Execute a coroutine with rate limiting."""
        current_time = time.time()
        time_since_last_batch = current_time - self.last_batch_time
        if time_since_last_batch < self.delay_between_batches:
            await asyncio.sleep(self.delay_between_batches - time_since_last_batch)

        async with self.semaphore:
            self.last_batch_time = time.time()
            return await coro

    async def execute_batch[T](
        self,
        coros: list[Coroutine[Any, Any, T]],
        batch_size: int | None = None,
        *,
        return_exceptions: bool = False,
    ) -> list[T | BaseException]:
        """Execute coroutines in controlled batches.

        With ``return_exceptions`` set, failures are returned in place of
        results rather than raised.
        """
        if batch_size is None:
            batch_size = self._max_concurrent

        results: list[T | BaseException] = []
        for i in range(0, len(coros), batch_size):
            batch = coros[i : i + batch_size]
            batch_results = await asyncio.gather(
                *[self.execute(coro) for coro in batch],
                return_exceptions=return_exceptions,
            )
            results.extend(batch_results)
        return results
