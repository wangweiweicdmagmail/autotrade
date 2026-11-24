import json
from console.services.rhythm import MarketRhythmReader


def fixture():
    return {"version": 1, "as_of": 180, "generated_at": 184, "day": "2026-11-18", "columns": ["time"],
            "scoring": {"model": "opening15_midpoint_v7", "version": 7, "stock_session": "regular", "pending": [], "ready": 1, "eligible": 1},
            "beta": ["NAS", "SPX"], "ranks": [{"symbol": "A"}], "coverage": {'total':1,'aligned':1,'missing':[]},
            "series": {"A": {"symbol": "A", "points": [[120, 1, 1, 1, 1, 1, 1, 1, 0, "waiting", 0],
                [180, 99, 99, 1, 99, 100, 98, 99, 0, "waiting", 0]], "events": [], "last_closed": 120}}}


def test_reader_waits_then_loads_read_only_common_time_and_selection(tmp_path):
    path = tmp_path / "rhythm.json"
    reader = MarketRhythmReader(path)
    assert reader.read(now=200)["status"] == "waiting"
    path.write_text(json.dumps(fixture()))
    before = path.read_bytes()
    result = reader.read("../../no", now=200)
    assert result["selected"] == "A"
    assert len(result["tracks"]) == 12
    assert result["tracks"][2]["points"] == [[120, 1, 1, 1, 1, 1, 1, 1, 0, "waiting", 0]]
    assert not result["tracks"][0]["aligned"]
    assert path.read_bytes() == before
    path.write_text('{broken')
    assert reader.read(now=201)["status"] == "error"
    assert reader.read(now=201)["tracks"] == []


def test_future_cache_and_unknown_versions_are_visible_errors(tmp_path):
    path = tmp_path / "rhythm.json"
    value = fixture()
    path.write_text(json.dumps(value))
    reader = MarketRhythmReader(path)
    assert reader.read(now=179)["status"] == "error"
    value["version"] = 9
    path.write_text(json.dumps(value))
    assert reader.read(now=200)["status"] == "error"


def test_startup_cache_without_a_confirmed_batch_remains_waiting(tmp_path):
    path = tmp_path / "rhythm.json"
    path.write_text(json.dumps({**fixture(), "as_of": 0, "series": {}, "ranks": []}))
    assert MarketRhythmReader(path).read(now=200)["status"] == "waiting"


def test_twelve_slots_keep_beta_first_and_the_ten_candidates_in_rank_order(tmp_path):
    value = fixture()
    sample = value["series"]["A"]
    value["series"] = {s: {**sample, "symbol": s} for s in ["NAS", "SPX", *[f"S{i}" for i in range(10)]]}
    value["ranks"] = [{"symbol": f"S{i}", "rank": i + 1, "direction_sign": 1 if i % 2 else -1} for i in range(10)]
    value['coverage']={'total':12,'aligned':12,'missing':[]}
    path = tmp_path / "rhythm.json"
    path.write_text(json.dumps(value))
    result = MarketRhythmReader(path).read("S5", now=200)
    assert [r["symbol"] for r in result["tracks"]] == ["NAS", "SPX", *[f"S{i}" for i in range(10)]]
    assert [r["rank"] for r in result["tracks"][2:]] == list(range(1, 11))
    assert all(r["aligned"] for r in result["tracks"])
    assert result["selected"] == "S5"


def test_manual_watch_reads_known_non_ranked_symbols_without_changing_the_top_ten(tmp_path):
    value = fixture()
    value['series']['B'] = {**value['series']['A'], 'symbol': 'B'}
    value['coverage']={'total':2,'aligned':2,'missing':[]}
    path = tmp_path / 'rhythm.json'
    path.write_text(json.dumps(value))
    result = MarketRhythmReader(path).read('B', watch='B,../../unknown,B,NAS', now=200)
    assert len(result['tracks']) == 12
    assert [t['symbol'] for t in result['watch_tracks']] == ['B']
    assert result['selected'] == 'B'
    assert result['ranks'] == value['ranks']
    assert len(result['watch_tracks'][0]['points']) == 1


def test_td_coloring_is_optional_and_never_exposes_a_future_count(tmp_path):
    value=fixture()
    value['series']['A']['td']={'version':1,'comparison_bars':4,'trend_threshold':3,
        'counts':[[120,3],[180,4],None,['invalid',5],[120], [float('nan'),6]]}
    path=tmp_path/'rhythm.json';path.write_text(json.dumps(value))
    result=MarketRhythmReader(path).read(now=200)
    track=result['tracks'][2]
    assert track['td']['counts']==[[120,3]]
    assert len(track['points'][0])==11
    value['series']['A']['td']='old optional field'
    path.write_text(json.dumps(value))
    assert MarketRhythmReader(path).read(now=200)['tracks'][2]['td']['counts']==[[120,3]]
    other=tmp_path/'optional.json';other.write_text(json.dumps(value))
    assert MarketRhythmReader(other).read(now=200)['tracks'][2]['td'] is None


def test_held_symbol_history_can_cover_ten_non_ranked_slots_without_changing_execution_ranks(tmp_path):
    value=fixture();sample=value['series']['A']
    for i in range(12):value['series'][f'H{i}']={**sample,'symbol':f'H{i}'}
    value['coverage']={'total':13,'aligned':13,'missing':[]}
    path=tmp_path/'rhythm.json';path.write_text(json.dumps(value))
    wanted=','.join(f'H{i}' for i in range(12))
    result=MarketRhythmReader(path).read('H9',watch=wanted,now=200)
    assert [r['symbol'] for r in result['watch_tracks']]==[f'H{i}' for i in range(10)]
    assert result['selected']=='H9' and result['ranks']==value['ranks']


def test_old_score_cache_waits_for_rebuild_and_pending_five_minute_facts_are_not_published(tmp_path):
    value=fixture();path=tmp_path/'rhythm.json';reader=MarketRhythmReader(path)
    value.pop('scoring');path.write_text(json.dumps(value))
    assert reader.read(now=200)['status']=='waiting'
    for version in (1,2):
        value['scoring']={'model':f'ema9_15m_v{version}','version':version,'pending':[],'ready':1,'eligible':1}
        path.write_text(json.dumps(value))
        result=reader.read(now=200)
        assert result['status']=='waiting' and result['ranks']==[]
    value['scoring']={'model':'opening15_midpoint_v7','version':7,'stock_session':'regular','pending':[{'symbol':'A','reason':'score_5m_pending'}], 'ready':0,'eligible':1}
    path.write_text(json.dumps(value));result=reader.read(now=200)
    assert result['status']=='waiting' and result['ranks']==[]
    assert '0/1' in result['message'] and 'A' in result['message']
    value['scoring']['pending']=[];value['scoring']['ready']=1
    path.write_text(json.dumps(value));assert reader.read(now=200)['status']=='ready'
