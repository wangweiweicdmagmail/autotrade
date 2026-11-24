"""Explicit database-only opening-range position research; no live service or broker."""
import argparse
import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path

from engine.ranking_replay import cached_input, day_bounds
from engine.research_archive import envelope
from engine.research_calendar import ET
from engine.research_store import ResearchStore, digest


def opening_position_inputs(store, day, time_et, symbols, default_symbols=()):
    plan, _, _ = day_bounds(day)
    at = int(datetime.strptime(day+' '+time_et, '%Y-%m-%d %H:%M').replace(tzinfo=ET).timestamp())
    if not plan['regular_open']+900 <= at <= plan['regular_close']:
        raise ValueError('固定开盘小区间仅在09:45之后可用于收盘排名')
    datasets, sources, missing = {}, {}, []
    for symbol in dict.fromkeys(symbols):
        if symbol in default_symbols:
            continue
        try:
            minute, rows, _ = cached_input(store, symbol, '1m', 'regular', day)
            if not minute:
                raise ValueError('missing_regular_1m_series')
            bars = [{k:b[k] for k in ('time','open','high','low','close','volume')}
                    for b in rows if plan['regular_open'] <= b['time'] and b['close_time'] <= at]
            data = {'bars':bars, 'metadata':{k:minute[k] for k in ('timezone','minmove','pricescale','type','session')}}
            identities = [minute['id']]
            five, rows, _ = cached_input(store, symbol, '5m', 'regular', day)
            if five:
                data['five_bars'] = [{k:b[k] for k in ('time','open','high','low','close','volume')}
                                     for b in rows if plan['regular_open'] <= b['time'] and b['close_time'] <= at]
                identities.append(five['id'])
            datasets[symbol] = data
            sources[symbol] = {'series_ids':identities, 'input_digest':digest(data),
                               'minute_rows':len(bars), 'five_rows':len(data.get('five_bars', []))}
        except ValueError as error:
            missing.append({'symbol':symbol, 'reason':str(error)})
    return {'day':day, 'as_of':at, 'datasets':datasets, 'sources':sources, 'missing':missing}


async def rank_opening_positions(store, day, time_et, symbols, default_symbols=()):
    inputs = opening_position_inputs(store, day, time_et, symbols, default_symbols)
    root = Path(__file__).resolve().parents[1]
    process = await asyncio.create_subprocess_exec('node', str(root/'service/opening_position_research.js'),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(process.communicate(json.dumps(inputs).encode()), 90)
    except (asyncio.CancelledError, TimeoutError):
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise ValueError(err.decode(errors='replace')[-1000:])
    result = json.loads(out)
    input_missing = [row for row in inputs['missing'] if row['symbol'] not in result['reference_symbols']]
    result['missing'] += input_missing
    result['eligible'] += len(input_missing)
    result['vwap_baseline']['missing'] += input_missing
    result['vwap_baseline']['complete'] = result['vwap_baseline']['complete'] and not input_missing
    result.update(time_et=time_et, mode='latest_research_reconstruction', sources=inputs['sources'],
                  requested_universe=list(symbols), universe_digest=digest(list(symbols)), database=str(store.path),
                  reader_source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    result['complete'] = result['complete'] and not input_missing
    source_names = ('indicators/opening-range-position.pine', 'service/opening_position_research.js', 'engine/opening_position_replay.py')
    source_bundle = {'kind':'opening_position_sources', 'model':result['model'],
                     'sources':{name:(root/name).read_text() for name in source_names}}
    result['source_bundle_id'] = digest(source_bundle)
    store.apply(envelope('definition', source_bundle))
    result['experiment_id'] = digest(result)
    store.apply(envelope('definition', {'kind':'opening_position_ranking_experiment', 'result':result}))
    return result


def save_report(result, folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"opening15-midpoint-half-sides-{result['day']}-{result['time_et'].replace(':','')}-{result['experiment_id'][:8]}"
    base = folder/stem
    lines = [f"# {result['day']} {result['time_et']} ET：开盘小区间归一化位置", '',
        '公式M=(H+L)/2，X=(已确认一分钟close−M)/((H−L)/2)。H/L为09:30–09:45最高／最低价；09:45锁定，零轴为中点，上沿+1、下沿−1。多头X>0按X降序，空头X<0按X升序，各取前十；0不进入任一方向榜。',
        '仅使用指定确认时刻前已收盘K线，本地PineTS运行独立Pine原文；事后重建，不代表历史实际接收榜单。',
        '观察指标不注册到实盘，不改变榜单、方向准入、保护或TV席位。首次可用为09:45，之前不回填最终区间。',
        f"就绪{len(result['ranks'])}只，完整状态：{result['complete']}；缺数："+json.dumps(result['missing'],ensure_ascii=False), '',
        '| 排名 | 股票 | 方向 | 新坐标X | 区间中点 | 开盘价 | 当前close | 区间宽 | 较开盘涨跌 | VWAP位置排名 |',
        '|---:|---|:---:|---:|---:|---:|---:|---:|---:|---:|']
    overall_header = lines[-2:]
    lines = lines[:-2]
    for label, key in (('多头前十', 'long_top10'), ('空头前十', 'short_top10')):
        lines += ['## '+label, '', '| 方向排名 | 股票 | 坐标X | 全池排名 |', '|---:|---|---:|---:|']
        for r in result[key]:
            lines.append(f"| {r['rank']} | {r['symbol']} | {r['coordinate']:+.4f} | {r['combined_rank']} |")
        if not result[key]:
            lines.append('该方向暂无有效候选。')
        lines += ['']
    lines += ['## 全池合并排名', '', *overall_header]
    for r in result['ranks']:
        direction = '多' if r['direction']>0 else '空' if r['direction']<0 else '中性'
        lines.append(f"| {r['rank']} | {r['symbol']} | {direction} | {r['coordinate']:+.4f} | {r['opening_midpoint']:.4f} | {r['day_open']:.4f} | {r['close']:.4f} | {r['opening_width']:.4f} | {r['move_pct']:+.3f}% | {r['vwap_position_rank'] or '缺数'} |")
    lines += ['', '## 同时刻VWAP纯位置基线', '', '| 排名 | 股票 | 方向 | Z |', '|---:|---|:---:|---:|']
    for r in result['vwap_baseline']['ranks'][:10]:
        lines.append(f"| {r['rank']} | {r['symbol']} | {'多' if r['direction']>0 else '空'} | {r['z']:+.4f} |")
    lines += ['', 'VWAP基线缺数：'+json.dumps(result['vwap_baseline']['missing'],ensure_ascii=False), '',
        'X为距区间中点多少个半宽，不是相对开盘的涨跌幅或速度；开仓仍由最新确认close与VWAP决定，不要求与榜单方向一致。',
        '源码归档ID：`'+result['source_bundle_id']+'`', '实验ID：`'+result['experiment_id']+'`', 'Pine源码哈希：`'+result['indicator_source_sha256']+'`', 'PineTS：'+result['pinets_version'], '']
    for suffix, content in (('.json', json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)), ('.md', '\n'.join(lines))):
        path = base.with_suffix(suffix)
        if path.exists() and path.read_text() != content:
            raise ValueError('实验报告已存在且内容不同，禁止覆盖')
        path.write_text(content)
    return str(base.with_suffix('.md'))


def main():
    import config
    parser = argparse.ArgumentParser(description='独立开盘小区间位置研究，不启动交易服务')
    parser.add_argument('--data-dir',type=Path,default=Path(__file__).resolve().parents[1]/'logs')
    parser.add_argument('--day',required=True)
    parser.add_argument('--at',required=True,help='美东收盘确认时刻HH:MM')
    args = parser.parse_args()
    result = asyncio.run(rank_opening_positions(ResearchStore(args.data_dir/'research/archive.sqlite3'),
                       args.day,args.at,config.WATCHLIST,config.TV_DEFAULT_SESSION_SYMBOLS))
    result['report_path'] = save_report(result,args.data_dir/'agent_research/experiments')
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))


if __name__ == '__main__':
    main()
