"""A single trend board is published; TD confirmation remains an exit/readout fact."""
from console.services.rhythm import MarketRhythmReader
from .test_ranking_snapshot import raw,put
from .test_candidate_entry_admission import NOW


def test_reader_does_not_publish_retired_board_or_tracks(tmp_path):
    path=tmp_path/'rhythm.json';data=raw();put(path,data)
    reader=MarketRhythmReader(path)
    frame=reader.read(now=NOW)
    assert frame['status']=='ready' and frame['ranks']
    assert 'td_ranking' not in frame and 'td_tracks' not in frame
    assert any(t.get('td') for t in frame['tracks'])
    put(path,raw(complete=False))
    held=reader.read(now=NOW+1)
    assert held['ranks']==frame['ranks']
    assert 'td_ranking' not in held
