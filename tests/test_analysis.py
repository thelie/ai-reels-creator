import asyncio

import numpy as np

from reels.analysis.beats import SR, analyze_music, detect_beats
from reels.analysis.pipeline import analyze_assets, analyze_reference_sync


def test_beats_on_click_track(media):
    info = analyze_music(str(media["music"]))
    assert abs(info.bpm - 120) < 3
    gaps = np.diff(info.beats)
    assert abs(np.median(gaps) - 0.5) < 0.03


def test_beats_silence():
    assert detect_beats(np.zeros(SR * 3, dtype=np.float32)).beats == []


def test_analyze_assets(media, tmp_path):
    items = [("v", media["multi"], "video", "multi.mp4"), ("p", media["photo"], "photo", "photo.jpg")]
    analyses, hook = asyncio.run(analyze_assets(items, tmp_path))
    by_id = {a.asset_id: a for a in analyses}
    video = by_id["v"]
    assert (video.width, video.height) == (1280, 720)
    assert len(video.segments) == 3  # три склеенных источника -> три сцены
    for s in video.segments:
        assert 0 <= s.quality <= 1 and 0 <= s.anchor_x <= 1
        assert s.keyframe and "multi.mp4" in s.description
    assert by_id["p"].segments[0].kind == "photo"
    assert hook == {}  # без LLM


def test_reference(media, tmp_path):
    ref = analyze_reference_sync(media["multi"], tmp_path)
    assert len(ref.shots) == 3
    assert abs(sum(s.dur for s in ref.shots) - 9.0) < 0.3
