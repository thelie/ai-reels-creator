"""Оркестратор: жизненный цикл проекта (сбор -> анализ -> план -> согласование -> рендер -> правки).

Не зависит от Telegram: бот и CLI вызывают одни и те же методы, прогресс сообщается колбэком.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from ..analysis import pipeline
from ..analysis.beats import analyze_music
from ..analysis.models import AssetAnalysis, ReferenceAnalysis
from ..config import Settings, get_settings
from ..edl.models import EDL, Canvas
from ..edl.validator import errors, validate
from ..llm import client as llm
from ..media import ffmpeg
from ..planning import heuristic, library, llm_planner
from ..planning.builder import MediaIndex, MusicInput, build_edl, pick_library_music
from ..planning.models import PlanDraft, StyleTemplate
from ..render import qa, renderer
from ..storage.db import Asset, Database, EdlVersion, Project, Render
from .states import BUSY, State, check

log = logging.getLogger(__name__)

Progress = Callable[[str], Awaitable[None]]


async def _noop(_: str) -> None:
    return None


class UserError(RuntimeError):
    """Ошибка, которую можно показать пользователю как есть."""


@dataclass
class Storyboard:
    text: str
    keyframes: list[str]


class ReelsService:
    def __init__(self, db: Database, settings: Settings | None = None):
        self.db = db
        self.settings = settings or get_settings()
        self._locks: dict[str, asyncio.Lock] = {}
        self._jobs = asyncio.Semaphore(self.settings.max_concurrent_jobs)

    # ---------- проекты и ассеты ----------

    def project_dir(self, pid: str) -> Path:
        d = self.settings.data_dir / "projects" / pid
        d.mkdir(parents=True, exist_ok=True)
        return d

    def lock(self, pid: str) -> asyncio.Lock:
        return self._locks.setdefault(pid, asyncio.Lock())

    def user_lock(self, tg_id: int) -> asyncio.Lock:
        # Альбом приходит несколькими апдейтами одновременно — без блокировки каждый создал бы свой проект
        return self._locks.setdefault(f"user:{tg_id}", asyncio.Lock())

    async def new_project(self, tg_id: int, chat_id: int = 0) -> Project:
        async with self.user_lock(tg_id):
            return await self._new_project(tg_id, chat_id)

    async def _new_project(self, tg_id: int, chat_id: int = 0) -> Project:
        async with self.db.session() as s:
            user = await self.db.get_or_create_user(s, tg_id)
            p = Project(user_id=user.id, chat_id=chat_id, duration=self.settings.default_duration_s,
                        state=State.COLLECTING)
            s.add(p)
            await s.commit()
            return await self.get(p.id)

    async def current_project(self, tg_id: int, chat_id: int = 0) -> Project:
        async with self.user_lock(tg_id):
            return await self._current_project(tg_id, chat_id)

    async def _current_project(self, tg_id: int, chat_id: int = 0) -> Project:
        async with self.db.session() as s:
            user = await self.db.get_or_create_user(s, tg_id)
            await s.commit()
            p = (
                await s.execute(
                    select(Project).where(Project.user_id == user.id).order_by(Project.created_at.desc()).limit(1)
                )
            ).scalar_one_or_none()
        if p is None:
            return await self._new_project(tg_id, chat_id)
        return p

    async def get(self, pid: str) -> Project:
        async with self.db.session() as s:
            p = await s.get(Project, pid)
            if p is None:
                raise UserError("Проект не найден")
            return p

    async def update(self, pid: str, **fields) -> Project:
        async with self.db.session() as s:
            p = await s.get(Project, pid)
            for k, v in fields.items():
                setattr(p, k, v)
            await s.commit()
        return await self.get(pid)

    async def set_state(self, pid: str, state: State, error: str = "") -> None:
        async with self.db.session() as s:
            p = await s.get(Project, pid)
            check(p.state, state)
            p.state = state
            p.error = error
            await s.commit()

    async def add_asset(self, pid: str, kind: str, src: Path, name: str = "", unique_id: str = "",
                        move: bool = True) -> Asset | None:
        """Кладёт файл в папку проекта. kind: video | photo | audio | reference | auto."""
        p = await self.get(pid)
        if unique_id and any(a.tg_file_unique_id == unique_id and a.kind == kind for a in p.assets):
            return None
        if len([a for a in p.assets if a.kind in ("video", "photo")]) >= self.settings.max_assets_per_project:
            raise UserError(f"В проекте уже {self.settings.max_assets_per_project} файлов — это максимум")
        if kind == "auto":
            info = await asyncio.to_thread(ffmpeg.probe, src)
            kind = info.kind
        inp = self.project_dir(pid) / "input"
        inp.mkdir(exist_ok=True)
        async with self.db.session() as s:
            a = Asset(project_id=pid, kind=kind, path="", orig_name=name or src.name, tg_file_unique_id=unique_id)
            s.add(a)
            await s.flush()
            dst = inp / f"{a.id}{src.suffix.lower() or '.bin'}"
            if move:
                shutil.move(str(src), dst)
            else:
                shutil.copy2(src, dst)
            a.path = str(dst)
            # Новый материал после планирования возвращает проект к сбору
            proj = await s.get(Project, pid)
            if proj.state not in (State.COLLECTING, *BUSY):
                proj.state = State.COLLECTING
            await s.commit()
            return a

    async def add_script(self, pid: str, text: str) -> Project:
        p = await self.get(pid)
        script = (p.script + "\n" + text).strip() if p.script else text.strip()
        return await self.update(pid, script=script[:4000])

    async def summary(self, pid: str) -> str:
        p = await self.get(pid)
        counts = {k: sum(1 for a in p.assets if a.kind == k) for k in ("video", "photo", "audio", "reference")}
        tpl = "авто" if p.template_id == "auto" else library.get(p.template_id).name
        lines = [
            f"🎬 Проект {p.id}",
            f"Видео: {counts['video']} · Фото: {counts['photo']}"
            + (f" · Музыка: {counts['audio']}" if counts["audio"] else "")
            + (" · Референс: есть" if counts["reference"] else ""),
            f"Длительность: {p.duration:.0f} с"
            + ("" if p.duration_set or not counts["reference"] else " (или как у референса)"),
            f"Стиль: {tpl}",
        ]
        if p.script:
            short = p.script if len(p.script) < 160 else p.script[:157] + "…"
            lines.append(f"Сценарий: {short}")
        return "\n".join(lines)

    # ---------- анализ ----------

    async def analyze(self, pid: str, progress: Progress = _noop) -> tuple[list[AssetAnalysis], dict[str, float]]:
        p = await self.get(pid)
        work = self.project_dir(pid) / "work"
        stored = (p.analysis or {}).get("assets", {})
        hook = dict((p.analysis or {}).get("hook", {}))
        media = [a for a in p.assets if a.kind in ("video", "photo")]
        if not media:
            raise UserError("Пришлите хотя бы одно фото или видео")
        todo = [(a.id, Path(a.path), a.kind, a.orig_name) for a in media if a.id not in stored]
        if todo:
            async def prog(done: int, total: int) -> None:
                await progress(f"🔍 Анализ материала {done}/{total}…")

            await progress(f"🔍 Анализ материала 0/{len(todo)}…")
            analyses, new_hook = await pipeline.analyze_assets(todo, work, concurrency=2, progress=prog)
            for a in analyses:
                stored[a.asset_id] = a.model_dump()
            hook.update(new_hook)
        # Референс
        ref_asset = next((a for a in reversed(p.assets) if a.kind == "reference"), None)
        reference = p.reference
        if ref_asset and (not reference or reference.get("_asset") != ref_asset.id):
            await progress("🎞 Разбираю референс…")
            ref = await asyncio.to_thread(pipeline.analyze_reference_sync, Path(ref_asset.path), work / "ref")
            ref = await llm_planner.describe_reference(ref)
            reference = ref.model_dump() | {"_asset": ref_asset.id}
        await self.update(pid, analysis={"assets": stored, "hook": hook}, reference=reference)
        analyses = [AssetAnalysis.model_validate(stored[a.id]) for a in media if a.id in stored]
        if not analyses:
            raise UserError("Не удалось прочитать ни один файл. Попробуйте прислать видео как файл (документ).")
        return analyses, hook

    # ---------- планирование ----------

    def _media_index(self, p: Project, analyses: list[AssetAnalysis]) -> MediaIndex:
        work = self.project_dir(p.id) / "work"
        paths: dict[str, tuple[str, str | None]] = {}
        by_id = {a.id: a for a in p.assets}
        for an in analyses:
            asset = by_id[an.asset_id]
            if an.kind == "photo":
                norm = work / f"{an.asset_id}_photo.jpg"
                paths[an.asset_id] = (str(norm), str(norm))
            else:
                proxy = work / f"{an.asset_id}_proxy.mp4"
                paths[an.asset_id] = (asset.path, str(proxy) if proxy.exists() else None)
        return MediaIndex({a.asset_id: a for a in analyses}, paths)

    def _music(self, p: Project) -> MusicInput | None:
        audio = next((a for a in reversed(p.assets) if a.kind == "audio"), None)
        path = Path(audio.path) if audio else pick_library_music(self.settings.music_dir)
        if not path:
            return None
        try:
            return MusicInput(str(path), analyze_music(str(path)))
        except ffmpeg.FFmpegError:
            log.warning("cannot decode music %s", path)
            return None

    def _resolve_template(self, p: Project, analyses: list[AssetAnalysis]) -> StyleTemplate | None:
        """None — пусть выберет LLM."""
        base = None if p.template_id == "auto" else library.get(p.template_id)
        if p.reference:
            ref = ReferenceAnalysis.model_validate({k: v for k, v in p.reference.items() if k != "_asset"})
            return library.from_reference(ref, base or library.auto_select(analyses, p.script))
        if base:
            return base
        return None if llm.available() else library.auto_select(analyses, p.script)

    def _target_duration(self, p: Project, template: StyleTemplate | None) -> float:
        if template and template.slots and not p.duration_set:
            return round(sum(s.dur for s in template.slots), 2)
        return p.duration

    async def plan(self, pid: str, progress: Progress = _noop, instruction: str | None = None,
                   variant: bool = False) -> Storyboard:
        """Анализ (если нужен) + план + EDL. instruction — правка текстом, variant — другой вариант."""
        async with self.lock(pid):
            p = await self.get(pid)
            if p.state in BUSY:
                raise UserError("Проект уже в работе, подождите")
            prev_state = p.state
            try:
                if p.state in (State.COLLECTING, State.FAILED):
                    await self.set_state(pid, State.ANALYZING)
                analyses, hook = await self.analyze(pid, progress)
                await self.set_state(pid, State.PLANNING)
                await progress("🧠 Составляю план ролика…")
                p = await self.get(pid)
                async with self._jobs:
                    edl, draft, template, duration, note = await self._make_plan(
                        p, analyses, hook, instruction, variant)
                await self._save_version(p, edl, draft, template, duration, note)
                await self.set_state(pid, State.AWAITING_APPROVAL)
            except UserError as e:
                await self._fail(pid, prev_state, str(e))
                raise
            except Exception as e:
                log.exception("planning failed")
                await self._fail(pid, prev_state, str(e))
                raise UserError("Не получилось составить план. Попробуйте ещё раз или добавьте материал.") from e
            return await self.storyboard(pid)

    async def _fail(self, pid: str, prev_state: str, msg: str) -> None:
        p = await self.get(pid)
        if p.state in BUSY:
            # После неудачной правки откатываемся к предыдущему рабочему состоянию
            fallback = State.FAILED if prev_state in (State.COLLECTING, State.FAILED) else State(prev_state)
            async with self.db.session() as s:
                proj = await s.get(Project, pid)
                proj.state = fallback
                proj.error = msg
                await s.commit()

    async def _make_plan(self, p: Project, analyses: list[AssetAnalysis], hook: dict[str, float],
                         instruction: str | None, variant: bool):
        media = self._media_index(p, analyses)
        music = await asyncio.to_thread(self._music, p)
        canvas = Canvas(w=self.settings.canvas_w, h=self.settings.canvas_h, fps=self.settings.fps)
        template = self._resolve_template(p, analyses)
        duration = self._target_duration(p, template)
        current = PlanDraft.model_validate(p.plan) if p.plan else None
        ref = ReferenceAnalysis.model_validate({k: v for k, v in p.reference.items() if k != "_asset"}) \
            if p.reference else None
        note = "план"
        seed = p.variant_seed + (1 if variant else 0)
        if variant:
            await self.update(p.id, variant_seed=seed)

        draft: PlanDraft | None = None
        if instruction:
            if not llm.available():
                raise UserError("Правки текстом требуют подключённой LLM (ANTHROPIC_API_KEY). "
                                "Пока доступны кнопки: другой вариант, длительность, стиль.")
            if current is None:
                raise UserError("Сначала соберите ролик")
            cur_tpl = self._template_from_saved(p) or library.get(current.template_id)
            try:
                draft, duration = await llm_planner.revise(current, instruction, analyses, duration, cur_tpl)
            except llm.LLMError as e:
                raise UserError(f"Не удалось применить правку: {e}") from e
            template = cur_tpl if cur_tpl.id == draft.template_id or cur_tpl.id == "reference" \
                else library.get(draft.template_id)
            note = f"правка: {instruction[:80]}"
        elif llm.available():
            extra = ""
            if variant and current:
                extra = ("Сделай ДРУГОЙ вариант, заметно отличающийся от предыдущего (другой хук, порядок, "
                         f"тексты). Предыдущий план: {current.model_dump_json()}")
            try:
                draft = await llm_planner.plan(analyses, duration, p.script, template, hook, ref, extra)
                note = "вариант (LLM)" if variant else "план (LLM)"
            except llm.LLMError as e:
                log.warning("LLM planning failed, falling back to heuristic: %s", e)
        if draft is None:
            template = template or library.auto_select(analyses, p.script)
            draft = heuristic.plan(analyses, template, duration, p.script, hook, seed=seed)
            note = "вариант" if variant else "план"
        if template is None or template.id != draft.template_id and template.id != "reference":
            template = library.get(draft.template_id)

        edl = build_edl(draft, media, template, duration, music, canvas)
        issues = validate(edl, duration)
        if errors(issues):
            raise RuntimeError("; ".join(str(i) for i in errors(issues)))
        for i in issues:
            log.info("EDL warning: %s", i)
        return edl, draft, template, duration, note

    def _template_from_saved(self, p: Project) -> StyleTemplate | None:
        return StyleTemplate.model_validate(p.template) if p.template else None

    async def _save_version(self, p: Project, edl: EDL, draft: PlanDraft, template: StyleTemplate,
                            duration: float, note: str) -> None:
        async with self.db.session() as s:
            proj = await s.get(Project, p.id)
            proj.edl_version += 1
            proj.plan = draft.model_dump()
            proj.template = template.model_dump()
            if not proj.duration_set:
                proj.duration = duration
            s.add(EdlVersion(project_id=p.id, version=proj.edl_version, edl=edl.model_dump(), plan=draft.model_dump(),
                             duration=duration, template=template.model_dump(), note=note))
            await s.commit()

    async def latest_edl(self, pid: str) -> tuple[int, EDL]:
        async with self.db.session() as s:
            row = (
                await s.execute(
                    select(EdlVersion).where(EdlVersion.project_id == pid).order_by(EdlVersion.version.desc()).limit(1)
                )
            ).scalar_one_or_none()
        if row is None:
            raise UserError("Сначала соберите ролик")
        return row.version, EDL.model_validate(row.edl)

    async def versions(self, pid: str) -> list[EdlVersion]:
        async with self.db.session() as s:
            return list((await s.execute(
                select(EdlVersion).where(EdlVersion.project_id == pid).order_by(EdlVersion.version))).scalars())

    async def undo(self, pid: str) -> Storyboard:
        """Откат к предыдущей версии плана (копией, история не теряется)."""
        vs = await self.versions(pid)
        if len(vs) < 2:
            raise UserError("Откатываться некуда")
        prev = vs[-2]
        async with self.db.session() as s:
            proj = await s.get(Project, pid)
            proj.edl_version += 1
            proj.plan = prev.plan
            proj.template = prev.template
            if not proj.duration_set:
                proj.duration = prev.duration
            proj.state = State.AWAITING_APPROVAL
            s.add(EdlVersion(project_id=pid, version=proj.edl_version, edl=prev.edl, plan=prev.plan,
                             duration=prev.duration, template=prev.template, note=f"откат к v{prev.version}"))
            await s.commit()
        return await self.storyboard(pid)

    async def storyboard(self, pid: str) -> Storyboard:
        p = await self.get(pid)
        _, edl = await self.latest_edl(pid)
        draft = PlanDraft.model_validate(p.plan)
        segs = {}
        for a in (p.analysis or {}).get("assets", {}).values():
            for s in a["segments"]:
                segs[s["id"]] = s
        tpl = StyleTemplate.model_validate(p.template) if p.template else library.get(draft.template_id)
        lines = [f"📋 План · {tpl.name} · {edl.duration:.1f} с · {len(edl.clips)} сцен"]
        if draft.hook_text:
            lines.append(f"Хук: «{draft.hook_text}»")
        keyframes = []
        scenes = [sc for sc in draft.scenes if sc.segment_id in segs]
        for i, (sc, clip) in enumerate(zip(scenes, edl.clips), 1):
            seg = segs[sc.segment_id]
            desc = seg.get("description", "")
            shown = sc.text if not (i == 1 and draft.hook_text) else ""  # на первой сцене виден хук
            txt = f" — «{shown}»" if shown else ""
            audio = " 🔊" if clip.keep_audio else ""
            lines.append(f"{i}. {clip.at:.1f}–{clip.end:.1f}с {desc[:60]}{txt}{audio}")
            if seg.get("keyframe") and len(keyframes) < 10 and seg["keyframe"] not in keyframes:
                keyframes.append(seg["keyframe"])
        if draft.cta_text:
            lines.append(f"Финал: «{draft.cta_text}»")
        if edl.music:
            names = {a.path: a.orig_name for a in p.assets}
            lines.append("🎵 Музыка: " + names.get(edl.music.path, Path(edl.music.path).name))
        else:
            lines.append("🎵 Без музыки (пришлите аудиофайл, чтобы добавить)")
        text = "\n".join(lines)
        if len(text) > 1000:
            text = text[:997] + "…"
        return Storyboard(text=text, keyframes=keyframes)

    # ---------- рендер ----------

    async def render(self, pid: str, quality: renderer.Quality = "preview", progress: Progress = _noop) -> Path:
        async with self.lock(pid):
            p = await self.get(pid)
            if p.state in BUSY:
                raise UserError("Проект уже в работе, подождите")
            prev_state = p.state
            if p.state in (State.COLLECTING, State.FAILED):
                raise UserError("Материал изменился — сначала соберите ролик заново")
            version, edl = await self.latest_edl(pid)
            await self.set_state(pid, State.RENDERING)
            out = self.project_dir(pid) / "renders" / f"v{version}_{quality}.mp4"
            try:
                if not out.exists():
                    await progress("🎬 Монтирую превью…" if quality == "preview" else "🎬 Рендерю финал в 1080p…")
                    t0 = time.monotonic()
                    async with self._jobs:
                        await asyncio.to_thread(
                            renderer.render, edl, out, self.project_dir(pid) / "work", quality,
                            self.settings.font_name, self.settings.fonts_dir, self.settings.preview_scale,
                        )
                        size = renderer.output_size(edl, quality, self.settings.preview_scale)
                        report = await asyncio.to_thread(qa.check, out, edl.duration, size)
                    if not report["ok"]:
                        log.warning("QA problems for %s: %s", out, report["problems"])
                    async with self.db.session() as s:
                        s.add(Render(project_id=pid, edl_version=version, quality=quality, path=str(out),
                                     seconds=time.monotonic() - t0, qa=report))
                        await s.commit()
                await self.set_state(pid, State.PREVIEW_SENT if quality == "preview" else State.DELIVERED)
            except Exception as e:
                log.exception("render failed")
                async with self.db.session() as s:
                    proj = await s.get(Project, pid)
                    proj.state = prev_state
                    proj.error = str(e)
                    await s.commit()
                raise UserError("Рендер не удался. Попробуйте ещё раз.") from e
            return out
