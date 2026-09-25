import shutil

import pytest

from reels.config import Settings
from reels.orchestrator.service import ReelsService, UserError
from reels.orchestrator.states import State
from reels.storage.db import Database


@pytest.fixture
async def svc(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", music_dir=tmp_path / "no_music")
    settings.data_dir.mkdir(parents=True)
    db = Database(settings.db_url)
    await db.init()
    yield ReelsService(db, settings)
    await db.close()


async def _add(svc, pid, path, kind, tmp_path):
    copy = tmp_path / f"in_{path.name}"
    shutil.copy2(path, copy)
    return await svc.add_asset(pid, kind, copy, path.name)


async def test_full_flow(svc, media, tmp_path):
    p = await svc.new_project(tg_id=42, chat_id=42)
    for key, kind in (("multi", "video"), ("photo", "photo"), ("music", "audio")):
        await _add(svc, p.id, media[key], kind, tmp_path)
    await svc.add_script(p.id, "Хук ролика\nВторая фраза")
    await svc.update(p.id, duration=8, duration_set=True)

    progress = []

    async def prog(msg):
        progress.append(msg)

    board = await svc.plan(p.id, prog)
    assert "Хук ролика" in board.text and board.keyframes
    assert (await svc.get(p.id)).state == State.AWAITING_APPROVAL
    assert any("Анализ" in m for m in progress)

    out = await svc.render(p.id, "preview", prog)
    assert out.exists()
    assert (await svc.get(p.id)).state == State.PREVIEW_SENT

    await svc.plan(p.id, prog, variant=True)
    versions = await svc.versions(p.id)
    assert [v.version for v in versions] == [1, 2]

    await svc.undo(p.id)
    assert len(await svc.versions(p.id)) == 3

    # Правка текстом без LLM — понятная ошибка, проект не ломается
    with pytest.raises(UserError):
        await svc.plan(p.id, prog, instruction="сделай короче")
    assert (await svc.get(p.id)).state == State.AWAITING_APPROVAL


async def test_render_requires_fresh_plan(svc, media, tmp_path):
    p = await svc.new_project(tg_id=5)
    await _add(svc, p.id, media["photo"], "photo", tmp_path)
    await svc.plan(p.id)
    await _add(svc, p.id, media["photo2"], "photo", tmp_path)  # новый материал -> снова сбор
    assert (await svc.get(p.id)).state == State.COLLECTING
    with pytest.raises(UserError):
        await svc.render(p.id, "preview")


async def test_plan_without_media(svc):
    p = await svc.new_project(tg_id=1)
    with pytest.raises(UserError):
        await svc.plan(p.id)
    assert (await svc.get(p.id)).state == State.FAILED


async def test_reference_flow(svc, media, tmp_path):
    p = await svc.new_project(tg_id=7)
    await _add(svc, p.id, media["short"], "video", tmp_path)
    await _add(svc, p.id, media["photo"], "photo", tmp_path)
    await _add(svc, p.id, media["multi"], "reference", tmp_path)
    board = await svc.plan(p.id)
    p = await svc.get(p.id)
    assert p.template["id"] == "reference"
    assert abs(p.duration - 9.0) < 0.5  # длительность берётся из референса
    assert board.text.count("\n") >= 3


async def test_duplicate_asset_ignored(svc, media, tmp_path):
    p = await svc.new_project(tg_id=9)
    copy1 = tmp_path / "x1.jpg"
    copy2 = tmp_path / "x2.jpg"
    shutil.copy2(media["photo"], copy1)
    shutil.copy2(media["photo"], copy2)
    assert await svc.add_asset(p.id, "photo", copy1, unique_id="u1")
    assert await svc.add_asset(p.id, "photo", copy2, unique_id="u1") is None


async def test_concurrent_updates_share_one_project(svc):
    import asyncio

    projects = await asyncio.gather(*(svc.current_project(tg_id=77, chat_id=77) for _ in range(8)))
    assert len({p.id for p in projects}) == 1


async def test_failing_progress_does_not_break_plan(svc, media, tmp_path):
    p = await svc.new_project(tg_id=11)
    await _add(svc, p.id, media["photo"], "photo", tmp_path)

    async def broken(msg):
        raise ConnectionError("telegram is down")

    board = await svc.plan(p.id, broken)
    assert board.text.startswith("📋")
