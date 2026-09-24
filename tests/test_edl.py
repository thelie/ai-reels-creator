from reels.edl.models import EDL, Reframe, Source, TextOverlay, Transition, VideoClip
from reels.edl.validator import errors, validate
from reels.render.ass import ass_color, ass_time, build_ass, wrap


def _edl(**kw) -> EDL:
    src = {"a": Source(asset_id="a", kind="video", path="a.mp4", duration=5, width=1080, height=1920)}
    clips = kw.pop("clips", [
        VideoClip(id="c1", asset_id="a", src_in=0, at=0, dur=2),
        VideoClip(id="c2", asset_id="a", src_in=2, at=1.7, dur=2, transition_in=Transition(type="fade", dur=0.3)),
    ])
    return EDL(sources=src, clips=clips, **kw)


def test_valid_edl_has_no_errors():
    edl = _edl(texts=[TextOverlay(at=0, dur=1.5, content="Привет")])
    assert errors(validate(edl)) == []
    assert abs(edl.duration - 3.7) < 1e-6


def test_detects_gap_and_range_and_unknown_asset():
    edl = _edl(clips=[
        VideoClip(id="c1", asset_id="a", src_in=4, at=0, dur=2),  # выходит за исходник
        VideoClip(id="c2", asset_id="a", at=2.5, dur=1),  # дыра
        VideoClip(id="c3", asset_id="zzz", at=3.5, dur=1),
    ])
    codes = {i.code for i in validate(edl)}
    assert {"src_out_of_range", "gap", "unknown_asset"} <= codes


def test_text_checks():
    edl = _edl(texts=[TextOverlay(at=3, dur=5, content="x", style="nope")])
    codes = {i.code for i in validate(edl)}
    assert {"unknown_text_style", "text_out_of_range"} <= codes


def test_upscale_is_warning():
    edl = EDL(
        sources={"a": Source(asset_id="a", kind="photo", path="a.jpg", width=400, height=300)},
        clips=[VideoClip(id="c1", asset_id="a", at=0, dur=2, reframe=Reframe(zoom_to=1.2))],
    )
    issues = validate(edl)
    assert [i.code for i in issues] == ["upscale"] and not errors(issues)


def test_ass_helpers():
    assert ass_color("#FF0000") == "&H000000FF"
    assert ass_time(61.234) == "0:01:01.23"
    assert all(len(line) <= 20 for line in wrap("очень длинный текст который надо перенести", 70, 800))
    ass = build_ass(_edl(texts=[TextOverlay(at=0, dur=1, content="Тест {x}")]), "DejaVu Sans")
    assert "PlayResY: 1920" in ass and "Тест (x)" in ass
