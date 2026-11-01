"""应用后台任务的单一所有者；显式登记、重复启动保护与等待退出。"""

import asyncio
import logging

logger = logging.getLogger(__name__)


class TaskRegistry:
    def __init__(self):
        self._tasks = {}

    def start(self, name, factory):
        current = self._tasks.get(name)
        if current is not None and not current.done():
            return current
        task = asyncio.create_task(factory(), name=name)
        self._tasks[name] = task
        task.add_done_callback(self._observe)
        return task

    @staticmethod
    def _observe(task):
        if not task.cancelled() and task.exception() is not None:
            error = task.exception()
            logger.error(
                "后台任务异常退出 %s",
                task.get_name(),
                exc_info=(type(error), error, error.__traceback__),
            )

    def status(self):
        return {
            name: {"done": task.done(), "cancelled": task.cancelled()}
            for name, task in self._tasks.items()
        }

    async def stop_all(self):
        current = asyncio.current_task()
        tasks = [task for task in self._tasks.values() if task is not current]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
