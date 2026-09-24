"""CLI: собрать ролик из папки с материалом без Telegram (для отладки и пакетной работы).

    reels make media/ --script "Хук\\nФраза 1\\nФраза 2" --duration 20 --out reel.mp4
    reels make media/ --reference ref.mp4 --music track.mp3 --template travel_vlog
    reels templates
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
import tempfile
from pathlib import Path

from .config import get_settings
from .media.ffmpeg import IMAGE_EXT
from .orchestrator.service import ReelsService, UserError
from .planning import library
from .storage.db import Database

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
AUDIO_EXT = {".mp3", ".m4a", ".wav", ".ogg", ".aac", ".flac"}


async def _make(args: argparse.Namespace) -> int:
    settings = get_settings()
    db = Database(settings.db_url)
    await db.init()
    svc = ReelsService(db, settings)
    p = await svc.new_project(tg_id=0)
    files = sorted(Path(args.media).iterdir()) if Path(args.media).is_dir() else [Path(args.media)]
    with tempfile.TemporaryDirectory() as tmp:
        for f in files:
            ext = f.suffix.lower()
            kinds = {**dict.fromkeys(IMAGE_EXT, "photo"), **dict.fromkeys(VIDEO_EXT, "video"),
                     **dict.fromkeys(AUDIO_EXT, "audio")}
            kind = kinds.get(ext)
            if kind is None:
                continue
            copy = Path(tmp) / f.name
            shutil.copy2(f, copy)
            await svc.add_asset(p.id, kind, copy, f.name)
        for extra, kind in ((args.reference, "reference"), (args.music, "audio")):
            if extra:
                copy = Path(tmp) / Path(extra).name
                shutil.copy2(extra, copy)
                await svc.add_asset(p.id, kind, copy)
    if args.script:
        await svc.add_script(p.id, args.script.replace("\\n", "\n"))
    fields = {"template_id": args.template}
    if args.duration:
        fields |= {"duration": args.duration, "duration_set": True}
    await svc.update(p.id, **fields)

    async def progress(msg: str) -> None:
        print(msg, file=sys.stderr)

    try:
        board = await svc.plan(p.id, progress)
        print(board.text)
        out = await svc.render(p.id, args.quality, progress)
    except UserError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    finally:
        await db.close()
    if args.out:
        shutil.copy2(out, args.out)
        out = Path(args.out)
    print(f"Готово: {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="reels")
    sub = ap.add_subparsers(dest="cmd", required=True)
    mk = sub.add_parser("make", help="собрать ролик из папки")
    mk.add_argument("media", help="папка с фото/видео (аудио в папке = музыка)")
    mk.add_argument("--script", default="", help="примерный сценарий (строки через \\n)")
    mk.add_argument("--duration", type=float, default=0)
    mk.add_argument("--template", default="auto", choices=["auto", *library.templates()])
    mk.add_argument("--reference", help="ролик-референс")
    mk.add_argument("--music", help="музыкальный трек")
    mk.add_argument("--quality", default="final", choices=["preview", "final"])
    mk.add_argument("--out", help="куда сохранить mp4")
    sub.add_parser("templates", help="список шаблонов стиля")
    args = ap.parse_args(argv)
    if args.cmd == "templates":
        for t in library.templates().values():
            print(f"{t.id:18} {t.name} — {t.description}")
        return 0
    return asyncio.run(_make(args))


if __name__ == "__main__":
    raise SystemExit(main())
