"""历史 EMA9 名称兼容层；公共执行模型位于 pine_execution。"""
import time  # 保留历史测试/扩展的时钟替换入口
from engine.pine_execution import (
    PineExecutionEngine, ExecutionInstance,
    DEFAULT_BREAKEVEN_R, DEFAULT_ADD_UNLOCK_R,
)


class PyramidEngine(PineExecutionEngine):
    """兼容现有 EMA9 参数、状态文件与调用路径。"""


PyramidInstance = ExecutionInstance
