"""执行生命周期状态；字符串保持兼容既有订单与状态文件。"""

# 实例状态
WATCHING = "WATCHING"  # 监控进场（tick 触发）
AWAITING_CONFIRM = "AWAITING_CONFIRM"  # 候选已产生，等待人工确认（尚未下单）
PENDING = "PENDING"  # 限价单已挂，等成交
ACTIVE = "ACTIVE"  # 持仓中（bar 收盘管理）
DONE = "DONE"  # 实例结束
