"""Rate limiter for batched async operations."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Coroutine
from typing import Any


class RateLimiter:
    """Run coroutines in sized batches with a delay between batches."""

    def __init__(self, max_concurrent: int = 5, delay_between_batches: float = 0.1) -> None:
        self._max_concurrent = max_concurrent
        self.delay_between_batches = delay_between_batches

    async def execute_batch[T](
        self,
        coros: list[Coroutine[Any, Any, T]],
        batch_size: int | None = None,
        *,
        return_exceptions: bool = False,
    ) -> list[T | BaseException]:
        """Execute coroutines in controlled batches.

        With return_exceptions set, failures are returned in place of
        results rather than raised. Unstarted coroutines are closed if the
        batch loop is cancelled or aborted.
        """
        if batch_size is None:
            batch_size = self._max_concurrent

        remaining = list(coros)
        results: list[T | BaseException] = []
        last_batch_time = 0.0
        try:
            while remaining:
                if last_batch_time:
                    elapsed = time.monotonic() - last_batch_time
                    if elapsed < self.delay_between_batches:
                        await asyncio.sleep(self.delay_between_batches - elapsed)

                batch = remaining[:batch_size]
                del remaining[:batch_size]
                batch_results = await asyncio.gather(*batch, return_exceptions=return_exceptions)
                last_batch_time = time.monotonic()
                results.extend(batch_results)
        finally:
            for coro in remaining:
                coro.close()
        return results
