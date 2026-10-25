# -*- coding: utf-8 -*-
"""live 测试：需要真实 TV 凭证/网络/运行中进程，默认套件不跑。

运行方式：python3 -m pytest tests/live/ -m live
（pytest.ini 默认 addopts 含 -m "not live"，此处显式覆盖）
"""
