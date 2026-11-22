"""Retired compatibility name; use the console K-line/PineTS ingestion layer."""
class CloudDataCollector:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("TV云指标采集已退役；请使用 run_console.sh 的 K线 + PineTS 本地计算链路")
