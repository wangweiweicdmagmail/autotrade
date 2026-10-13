# -*- coding: utf-8 -*-
"""买回踩规则 v1 泛化回测 v2 — 全部使用图表精确的指标线值
输入: /tmp/{sym}_full.json (CDP 从图表读出的 bars + MA1/MA2 + 双ST + VWAP)
"""
import json
import sys
from datetime import datetime, timezone, timedelta

ET = timezone(timedelta(hours=-4))
DAY = '2026-10-02'


def supertrend_calc(rows, period=10, factor=3.0):
    up = dn = trend = rma = prev_c = None
    out = []
    for r in rows:
        h, l, c = r['h'], r['l'], r['c']
        tr = h - l if prev_c is None else max(h - l, abs(h - prev_c), abs(l - prev_c))
        rma = tr if rma is None else (rma * (period - 1) + tr) / period
        hl2 = (h + l) / 2
        ub, db = hl2 - factor * rma, hl2 + factor * rma
        if up is None:
            up, dn, trend = ub, db, 1
        else:
            up = max(ub, up) if prev_c > up else ub
            dn = min(db, dn) if prev_c < dn else db
            if c > dn: trend = 1
            elif c < up: trend = -1
        out.append(up if trend == 1 else dn)
        prev_c = c
    return out


def atr14(rows, period=14):
    rma = prev_c = None
    out = []
    for r in rows:
        h, l, c = r['h'], r['l'], r['c']
        tr = h - l if prev_c is None else max(h - l, abs(h - prev_c), abs(l - prev_c))
        rma = tr if rma is None else (rma * (period - 1) + tr) / period
        out.append(rma)
        prev_c = c
    return out


def pick_5m_st(rows):
    """stA/stB 中选出与 5M supertrend(10,3) 吻合的那条"""
    theory = supertrend_calc(rows)
    best = None
    for tag in ('A', 'B'):
        diffs = []
        for i, r in enumerate(rows):
            actual = r.get(f'st{tag}up') if r.get(f'st{tag}up') is not None else r.get(f'st{tag}dn')
            if actual is not None:
                diffs.append(abs(actual - theory[i]))
        if diffs:
            m = sum(diffs) / len(diffs)
            if best is None or m < best[0]:
                best = (m, tag)
    return best[1]


def et_hm(ts):
    d = datetime.fromtimestamp(ts, ET)
    return d.hour * 60 + d.minute


def analyze(rows):
    tag = pick_5m_st(rows)
    a14 = atr14(rows)
    trades, pos = [], None
    for i in range(1, len(rows)):
        r = rows[i]
        d = datetime.fromtimestamp(r['t'], ET)
        if d.date().isoformat() != DAY:
            continue
        hm = et_hm(r['t'])
        if pos:
            pos = tick_exit(pos, r, a14[i], trades, hm)
            continue
        if not (9 * 60 + 30 <= hm < 15 * 60 + 55):
            continue
        ma1, ma2, vw = r.get('ma1'), r.get('ma2'), r.get('vwap')
        st = r.get(f'st{tag}up') if r.get(f'st{tag}up') is not None else r.get(f'st{tag}dn')
        st_dir = 1 if r.get(f'st{tag}up') is not None else -1
        if not all(v is not None for v in (ma1, ma2, vw, st)):
            continue
        if ma1 <= ma2:
            continue  # 顺势过滤
        lines = [('EMA20', ma1), ('ST5', st), ('VWAP', vw), ('EMA100', ma2)]
        pierced = [(v, name) for name, v in lines if r['l'] < v]
        if not pierced:
            continue
        deep_v, deep_name = min(pierced)
        if r['c'] <= deep_v:
            continue
        # 回踩方向过滤: 信号线必须与线相交(影线跨线), 整根在线下方向上顶不算回踩
        if r['h'] <= deep_v:
            continue
        # ST 做空侧时线值在上方, 不构成支撑
        body = abs(r['c'] - r['o'])
        lw = min(r['o'], r['c']) - r['l']
        uw = r['h'] - max(r['o'], r['c'])
        hammer = lw >= 2 * max(body, 1e-9) and uw <= 0.3 * max(body, 1e-9)
        pos = {'entry': r['c'], 'stop': r['l'] - 0.05, 'line': deep_name,
               'hammer': hammer, 'trail': None, 'max_h': r['c'],
               'risk': r['c'] - (r['l'] - 0.05),
               'entry_t': d.strftime('%H:%M')}
    return trades


def tick_exit(pos, r, atr, trades, hm):
    line_map_keys = {'EMA20': 'ma1', 'VWAP': 'vwap', 'EMA100': 'ma2'}
    pos['max_h'] = max(pos['max_h'], r['h'])
    # TRAIL 2xATR, 浮盈达到 2R 后激活 (1R 太敏感, 信号线自己的高点就能触发)
    if pos['max_h'] >= pos['entry'] + 2 * pos['risk']:
        t = pos['max_h'] - 2 * atr
        pos['trail'] = t if pos['trail'] is None else max(pos['trail'], t)
    reason = price = None
    if r['l'] <= pos['stop']:
        reason, price = '止损', pos['stop']
    elif pos['trail'] is not None and r['l'] <= pos['trail']:
        reason, price = 'TRAIL', pos['trail']
    else:
        if pos['line'] in line_map_keys:
            lv = r.get(line_map_keys[pos['line']])
        else:
            lv = r.get('stBup') or r.get('stBdn') or r.get('stAup') or r.get('stAdn')
            # 注意: 此处取的是 5M ST 的当前线值; 多->空翻向后线跑到上方,
            # 收破判定只对仍在线下方一侧有效, 简化: close < 当前线值 即视为收破
        if lv is not None and r['c'] < lv:
            reason, price = f"收破{pos['line']}", r['c']
        elif hm >= 15 * 60 + 55:
            reason, price = '收盘了结', r['c']
    if reason:
        pnl = price - pos['entry']
        pos.update({'exit': price, 'reason': reason, 'pnl': pnl,
                    'rr': pnl / pos['risk'] if pos['risk'] > 0 else 0,
                    'exit_t': datetime.fromtimestamp(r['t'], ET).strftime('%H:%M')})
        trades.append(pos)
        return None
    return pos


def report(sym, path):
    rows = json.load(open(path))
    for r in rows:  # 旧版 dump 的键名兼容: st5*->stA*, st1*->stB*
        for old, new in (('st5up', 'stAup'), ('st5dn', 'stAdn'),
                         ('st1up', 'stBup'), ('st1dn', 'stBdn')):
            if old in r and new not in r:
                r[new] = r[old]
    trades = analyze(rows)
    print(f'\n===== {sym} =====')
    if not trades:
        print('  无信号')
        return []
    for t in trades:
        print(f"  {t['entry_t']} 入{t['entry']:.2f} 踩{t['line']}({'锤子' if t['hammer'] else '普通'}) "
              f"损{t['stop']:.2f} -> {t['exit_t']} 出{t['exit']:.2f} [{t['reason']}] "
              f"PnL {t['pnl']:+.2f} RR {t['rr']:+.1f}")
    wins = sum(1 for t in trades if t['pnl'] > 0)
    tot = sum(t['pnl'] for t in trades)
    print(f"  共{len(trades)}笔 胜{wins} 合计PnL {tot:+.2f} 平均RR {sum(t['rr'] for t in trades)/len(trades):+.1f}")
    return trades


if __name__ == '__main__':
    all_trades = {}
    for sym in ('tsm', 'tsla', 'arm', 'spcx'):
        all_trades[sym.upper()] = report(sym.upper(), f'/tmp/{sym}_full.json')
    print('\n===== 汇总 =====')
    for sym, ts in all_trades.items():
        if ts:
            wins = sum(1 for t in ts if t['pnl'] > 0)
            print(f"  {sym}: {len(ts)}笔 胜率{wins}/{len(ts)} PnL {sum(t['pnl'] for t in ts):+.2f}")
