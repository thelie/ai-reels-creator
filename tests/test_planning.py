import asyncio

import pytest

from reels.analysis.beats import analyze_music
from reels.analysis.models import ReferenceAnalysis, ReferenceShot
from reels.analysis.pipeline import analyze_assets
from reels.edl.validator import errors, validate
from reels.planning import heuristic, library
from reels.planning.builder import MediaIndex, MusicInput, build_edl

SCRIPT = "Выходные в Казани\nКремль на закате\nЕда на рынке\nВид с набережной\nПодпишись на канал"


@pytest.fixture(scope="module")
def analyzed(media, tmp_path_factory):
    work = tmp_path_factory.mktemp("work")
    items = [("v1", media["multi"], "video"), ("v2", media["short"], "video"),
             ("p1", media["photo"], "photo"), ("p2", media["photo2"], "photo")]
    analyses, hook = asyncio.run(analyze_assets(items, work))
    paths = {}
    for aid, path, kind in items:
        if kind == "photo":
            norm = str(work / f"{aid}_photo.jpg")
            paths[aid] = (norm, norm)
        else:
            paths[aid] = (str(path), str(work / f"{aid}_proxy.mp4"))
    return analyses, MediaIndex({a.asset_id: a for a in analyses}, paths)


def test_templates_load():
    ts = library.templates()
    assert len(ts) >= 7 and "dynamic_montage" in ts


def test_split_script():
    assert heuristic.split_script("Один. Два! Три?") == ["Один.", "Два!", "Три?"]
    assert heuristic.split_script("- a\n- b") == ["a", "b"]


@pytest.mark.parametrize("tid", list(library.templates()))
def test_every_template_builds_valid_edl(analyzed, media, tid):
    analyses, mi = analyzed
    tpl = library.get(tid)
    draft = heuristic.plan(analyses, tpl, 15, SCRIPT)
    music = MusicInput(str(media["music"]), analyze_music(str(media["music"])))
    edl = build_edl(draft, mi, tpl, 15, music)
    issues = validate(edl, 15)
    assert errors(issues) == [], issues
    assert abs(edl.duration - 15) < 2.5
    assert edl.texts and edl.texts[0].content == "Выходные в Казани"
    ids = [c.asset_id + str(c.src_in) for c in edl.clips]
    assert all(a != b for a, b in zip(ids, ids[1:])), "один и тот же кадр подряд"


def test_cuts_snap_to_beats(analyzed, media):
    analyses, mi = analyzed
    tpl = library.get("dynamic_montage")
    beats = analyze_music(str(media["music"]))
    edl = build_edl(heuristic.plan(analyses, tpl, 10), mi, tpl, 10, MusicInput(str(media["music"]), beats))
    grid = [b - edl.music.src_in for b in beats.beats]
    cut_times = [c.at + (c.transition_in.dur if c.transition_in else 0) for c in edl.clips[1:]]
    on_beat = sum(1 for t in cut_times if min(abs(t - g) for g in grid) < 0.03)
    assert on_beat >= len(cut_times) * 0.7


def test_variant_seed_changes_plan(analyzed):
    analyses, _ = analyzed
    tpl = library.get("travel_vlog")
    plans = {tuple(s.segment_id for s in heuristic.plan(analyses, tpl, 15, seed=s).scenes) for s in range(6)}
    assert len(plans) > 1


def test_reference_template(analyzed):
    analyses, mi = analyzed
    ref = ReferenceAnalysis(duration=6, shots=[ReferenceShot(start=i * 1.5, dur=1.5) for i in range(4)],
                            bpm=120, cut_on_beat_ratio=0.8)
    tpl = library.from_reference(ref)
    assert tpl.id == "reference" and len(tpl.slots) == 4 and tpl.cut_on_beat
    draft = heuristic.plan(analyses, tpl, 6)
    assert len(draft.scenes) == 4
    edl = build_edl(draft, mi, tpl, 6)
    assert errors(validate(edl, 6)) == []


def test_reference_sticker_labels_and_effects(analyzed):
    from reels.analysis.models import OverlayText

    analyses, mi = analyzed
    ref = ReferenceAnalysis(
        duration=6, shots=[ReferenceShot(start=i * 1.5, dur=1.5, effect="bw" if i == 0 else "none") for i in range(4)],
        overlay_texts=[OverlayText(text="Девочки, ну это\nНАХОДКА", role="persistent_title"),
                       OverlayText(text="немного ASMR", role="section_label")],
        has_captions=False)
    tpl = library.from_reference(ref)
    assert tpl.text.hook_mode == "sticker" and tpl.text.body_style == "label_script"
    assert not tpl.captions.enabled and tpl.slots[0].effect == "bw"
    draft = heuristic.plan(analyses, tpl, 6, "Готовлю раз\\nНА НЕДЕЛЮ")
    assert draft.scenes[0].effect == "bw"
    draft = draft.model_copy(update={"hook_text": "Готовлю раз\nНА НЕДЕЛЮ"})
    edl = build_edl(draft, mi, tpl, 6)
    sticker = [t for t in edl.texts if t.style.startswith("sticker_")]
    assert [t.content for t in sticker] == ["Готовлю раз", "НА НЕДЕЛЮ"]
    assert all(t.at == 0 and abs(t.at + t.dur - edl.duration) < 0.05 for t in sticker)
    assert edl.clips[0].effect == "bw"
    assert errors(validate(edl, 6)) == []
