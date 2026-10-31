import json

from service import refresh_pine_texts as tool


def test_same_version_normalizes_trailing_zero():
    assert tool.same_version("16.0", "16")
    assert tool.same_version("v1.0.0", "1")
    assert not tool.same_version("14.0", "16.0")
    assert not tool.same_version("", "16.0")


def packet(pine_id="USER;test", version="16.0", text="compiled"):
    return json.dumps({
        "m": "create_study",
        "p": ["cs", "st", "st1", "sds", "Script@tv-scripting-101!", {
            "pineId": pine_id, "pineVersion": version, "text": text,
        }],
    }, separators=(",", ":"))


def test_parse_study_frames_accepts_matching_version():
    required = {"USER;test": "16"}
    found = tool.parse_study_frames("~m~1~m~" + packet() + "~m~", required)
    assert found == {"USER;test": {"version": "16", "text": "compiled"}}


def test_parse_study_frames_rejects_stale_or_empty_text():
    required = {"USER;test": "16.0"}
    assert tool.parse_study_frames(packet(version="14.0"), required) == {}
    assert tool.parse_study_frames(packet(text=""), required) == {}


def test_atomic_write_can_restore_previous_cache(tmp_path, monkeypatch):
    path = tmp_path / "pine_texts.json"
    monkeypatch.setattr(tool, "OUT_PATH", path)
    tool.atomic_write({"old": True})
    tool.atomic_write({"new": True})
    assert json.loads(path.read_text()) == {"new": True}
    tool.atomic_write({"old": True})
    assert json.loads(path.read_text()) == {"old": True}
    assert path.stat().st_mode & 0o777 == 0o600
