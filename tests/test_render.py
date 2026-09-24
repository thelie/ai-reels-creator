import asyncio

from reels.analysis.pipeline import analyze_assets
from reels.edl.models import EDL, Captions, CaptionWord, Music, Reframe, Source, TextOverlay, Transition, VideoClip
from reels.media.ffmpeg import probe
from reels.render import qa, renderer


def test_render_preview_and_final(media, tmp_path):
    v = probe(media["short"])
    p = probe(media["photo"])
    edl = EDL(
        sources={
            "v": Source(asset_id="v", kind="video", path=str(media["short"]), duration=v.duration, width=v.width,
                        height=v.height, has_audio=True),
            "p": Source(asset_id="p", kind="photo", path=str(media["photo"]), width=p.width, height=p.height),
        },
        clips=[
            VideoClip(id="c1", asset_id="v", src_in=0.5, at=0, dur=1.5, keep_audio=True,
                      reframe=Reframe(anchor_x=0.3, zoom_to=1.1)),
            VideoClip(id="c2", asset_id="p", at=1.3, dur=1.5, transition_in=Transition(type="whip", dur=0.2),
                      reframe=Reframe(zoom_to=1.15)),
            VideoClip(id="c3", asset_id="v", src_in=2, at=2.8, dur=1.0, speed=1.5),
        ],
        texts=[TextOverlay(at=0, dur=1.3, content="Проверка текста")],
        captions=Captions(words=[CaptionWord(t=0.2, d=0.4, w="раз"), CaptionWord(t=0.6, d=0.4, w="два")]),
        music=Music(path=str(media["music"])),
        color="warm",
    )
    for quality, size in (("preview", (540, 960)), ("final", (1080, 1920))):
        out = renderer.render(edl, tmp_path / f"{quality}.mp4", tmp_path / "work", quality)
        report = qa.check(out, edl.duration, size)
        assert report["ok"], report
        assert -17 < report["loudness_lufs"] < -11


def test_clip_cache_reused(media, tmp_path):
    v = probe(media["vertical"])
    src = Source(asset_id="v", kind="video", path=str(media["vertical"]), duration=v.duration, width=v.width,
                 height=v.height)
    edl = EDL(sources={"v": src}, clips=[VideoClip(id="c1", asset_id="v", at=0, dur=1.0)])
    renderer.render(edl, tmp_path / "a.mp4", tmp_path / "w", "preview")
    cached = list((tmp_path / "w" / "clip_cache").glob("*.mp4"))
    edl2 = edl.model_copy(update={"texts": [TextOverlay(at=0, dur=0.9, content="новый текст")]})
    renderer.render(edl2, tmp_path / "b.mp4", tmp_path / "w", "preview")
    assert list((tmp_path / "w" / "clip_cache").glob("*.mp4")) == cached  # клипы не перерендерились


def test_vertical_source_keeps_orientation(media, tmp_path):
    analyses, _ = asyncio.run(analyze_assets([("v", media["vertical"], "video")], tmp_path))
    assert (analyses[0].width, analyses[0].height) == (720, 1280)
