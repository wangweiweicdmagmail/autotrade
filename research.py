"""Offline Agent research CLI. No application boot, broker or execution writes.

python research.py --data-dir /path/to/data daily --day 2026-11-20
"""
import argparse
import asyncio
import json
import sqlite3
from pathlib import Path

from engine.research_archive import ResearchArchive
from engine.research_methods import ResearchNotes
from engine.research_review import ResearchReview


def main(argv=None):
    parser = argparse.ArgumentParser(description='AutoTrade 本地研究，只读实盘账本')
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).resolve().parent/'logs')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('sync')
    p = commands.add_parser('data-status');p.add_argument('--day', required=True)
    p = commands.add_parser('bars');p.add_argument('--symbol', required=True);p.add_argument('--interval', choices=['1m','5m','1d'], required=True)
    p.add_argument('--series-id');p.add_argument('--start', type=int, required=True);p.add_argument('--end', type=int, required=True)
    p.add_argument('--as-of', type=float);p.add_argument('--limit', type=int, default=2000)
    p = commands.add_parser('rankings');p.add_argument('--day', required=True);p.add_argument('--at',type=int);p.add_argument('--as-of',type=float)
    p = commands.add_parser('trade-review');p.add_argument('--group',required=True);p.add_argument('--questions',action='store_true')
    p = commands.add_parser('daily');p.add_argument('--day')
    p = commands.add_parser('questions');p.add_argument('--status', default='pending');p.add_argument('--limit',type=int,default=20);p.add_argument('--offset',type=int,default=0)
    p = commands.add_parser('answer');p.add_argument('--question',required=True);p.add_argument('--text-file',type=Path,required=True)
    p.add_argument('--source',required=True);p.add_argument('--confirm',action='store_true')
    p = commands.add_parser('asked');p.add_argument('--question',required=True)
    commands.add_parser('methods')
    p = commands.add_parser('method');p.add_argument('--text-file',type=Path,required=True);p.add_argument('--source',required=True)
    p.add_argument('--question',action='append',default=[]);p.add_argument('--status',choices=['hypothesis','user_confirmed','validated'],default='hypothesis');p.add_argument('--method-id')
    p = commands.add_parser('backfill');p.add_argument('--day',required=True);p.add_argument('--credentials',type=Path,required=True);p.add_argument('--max-requests',type=int,default=4)
    p = commands.add_parser('cache-ranking-day');p.add_argument('--day',required=True);p.add_argument('--credentials',type=Path,required=True);p.add_argument('--concurrency',type=int,default=3)
    p = commands.add_parser('rank-at');p.add_argument('--day',required=True);p.add_argument('--at',required=True,help='美东收盘确认时刻 HH:MM')
    p = commands.add_parser('backup');p.add_argument('--to',type=Path,required=True)
    args = parser.parse_args(argv)
    archive = ResearchArchive(args.data_dir/'research')
    for _ in range(20):
        if not archive.pump():
            break
    store = archive.store
    notes = ResearchNotes(args.data_dir/'agent_research')
    review = ResearchReview(args.data_dir)
    if args.command == 'sync':
        result = review.sync()
    elif args.command == 'data-status':
        result = store.manifest(args.day) if store.path.exists() else {'available':False,'complete':False,'day':args.day}
    elif args.command == 'bars':
        series = store.series(args.symbol,args.interval)
        if args.series_id:
            series = [r for r in series if r['id']==args.series_id]
        if len(series)!=1:
            raise ValueError('行情来源或时段不唯一，请指定series-id；可用序列: '+json.dumps(series,ensure_ascii=False))
        result = {'series':series[0],'mode':'observed' if args.as_of is not None else 'latest_research',
                  'bars':store.bars(series[0]['id'],args.start,args.end,as_of=args.as_of,limit=args.limit)}
    elif args.command == 'rankings':
        result = store.rankings(args.day,at=args.at,as_of=args.as_of)
    elif args.command in ('trade-review','daily'):
        review.sync()
        result = review.trade(args.group,generate_questions=args.questions) if args.command=='trade-review' else review.daily(args.day)
        if args.command=='daily':
            result = {k:v for k,v in result.items() if k not in ('reviews','data_status')}
            report_path = notes.root/'reviews'/(result['day']+'.md')
            result['report_path'] = str(report_path) if report_path.exists() else None
            result['pending_questions'] = result['pending_questions'][:20]
    elif args.command == 'questions':
        if args.offset<0 or not 1<=args.limit<=100:
            raise ValueError('问题分页无效')
        rows = notes.questions(None if args.status=='all' else args.status)
        result = {'total':len(rows),'rows':rows[args.offset:args.offset+args.limit]}
    elif args.command == 'answer':
        result = notes.update_question(args.question,status='confirmed' if args.confirm else 'answered',
                                       text=args.text_file.read_text().strip(),source=args.source)
    elif args.command == 'asked':
        result = notes.update_question(args.question,status='asked')
    elif args.command == 'methods':
        result = notes.methods()
    elif args.command == 'method':
        result = notes.method(rule=args.text_file.read_text().strip(),source=args.source,question_ids=args.question,status=args.status,method_id=args.method_id)
    elif args.command == 'backfill':
        from engine.research_backfill import repair_day
        if not 1<=args.max_requests<=100:
            raise ValueError('补取请求数无效')
        result = asyncio.run(repair_day(store,args.day,args.credentials,max_requests=args.max_requests))
    elif args.command in ('cache-ranking-day','rank-at'):
        import config
        from engine.ranking_replay import cache_day,rank_at
        result=asyncio.run(cache_day(store,args.day,config.WATCHLIST,config.TV_DEFAULT_SESSION_SYMBOLS,args.credentials,concurrency=args.concurrency)) if args.command=='cache-ranking-day' else asyncio.run(rank_at(store,args.day,args.at,config.WATCHLIST,config.TV_DEFAULT_SESSION_SYMBOLS))
    elif args.command == 'backup':
        store.backup(args.to);result={'ok':True,'path':str(args.to)}
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))
    return result


if __name__=='__main__':
    try:
        main()
    except (OSError,ValueError,KeyError,sqlite3.Error) as error:
        import sys
        print(json.dumps({'error':str(error)},ensure_ascii=False),file=sys.stderr)
        raise SystemExit(1)
