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


def test_split_by_speech_cuts_at_pauses():
    from reels.analysis.models import Word
    from reels.analysis.pipeline import _split_by_speech

    words = []
    t = 0.5
    for _phrase in range(4):  # 4 фразы по 6 слов, между фразами пауза 0.6 с
        for _ in range(6):
            words.append(Word(t=round(t, 2), d=0.3, w="слово"))
            t += 0.35
        t += 0.6
    chunks = _split_by_speech(0, t + 1, words)
    assert chunks and all(e - s <= 7.5 for s, e in chunks)
    # Ни одна граница не попадает внутрь слова
    for s, e in chunks:
        for w in words:
            assert not (w.t < s < w.t + w.d) and not (w.t < e < w.t + w.d)
    assert _split_by_speech(0, 10, words[:2]) is None


def test_long_phrase_without_pauses_is_split():
    from reels.analysis.models import Word
    from reels.analysis.pipeline import MAX_SPEECH_SEGMENT_S, _split_by_speech

    # 40 слов подряд без пауз (~12 с), одна чуть большая пауза посередине
    words = [Word(t=round(i * 0.3 + (0.2 if i >= 20 else 0), 2), d=0.28, w="слово") for i in range(40)]
    chunks = _split_by_speech(0, 20, words)
    assert len(chunks) >= 2 and all(e - s <= MAX_SPEECH_SEGMENT_S + 0.5 for s, e in chunks)
