import asyncio

class Timer:
    def __init__(self, period, callback):
        self._period = period
        self._callback = callback
        self._running = True
        self._task = asyncio.create_task(self._job())

    async def _job(self):
        while self._running:
            await asyncio.sleep(self._period)
            await self._callback()

    def cancel(self):
        self._running = False
        self._task.cancel()
