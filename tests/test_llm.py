"""LLM-часть без реальных запросов: форма запроса (локальный mock-сервер) и откат на эвристики."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from reels.analysis.models import AssetAnalysis, Segment
from reels.llm import client as llm
from reels.planning import llm_planner
from reels.planning.models import PlanDraft, PlanScene

ANALYSES = [AssetAnalysis(asset_id="a", kind="video", duration=5, width=1920, height=1080,
                          segments=[Segment(id="a_s00", asset_id="a", kind="video", start=0, end=5,
                                            description="кремль")])]
PLAN = {"template_id": "travel_vlog", "title": "t", "hook_text": "Хук",
        "scenes": [{"segment_id": "a_s00", "role": "hook", "duration": 2, "text": "", "keep_audio": False},
                   {"segment_id": "missing", "role": "body", "duration": 2, "text": "x", "keep_audio": False}],
        "cta_text": "", "caption": "c", "hashtags": ["казань"], "color": "warm"}


@pytest.fixture
def mock_api(monkeypatch):
    captured: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            captured["body"], captured["headers"] = body, dict(self.headers)
            resp = {"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                    "content": [{"type": "text", "text": json.dumps(PLAN, ensure_ascii=False)}],
                    "stop_reason": "end_turn", "stop_sequence": None,
                    "usage": {"input_tokens": 10, "output_tokens": 20}}
            data = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setattr(llm, "_client", None)
    yield captured
    srv.shutdown()
    llm._client = None


async def test_plan_request_shape(mock_api):
    draft = await llm_planner.plan(ANALYSES, 15, "сценарий")
    assert [s.segment_id for s in draft.scenes] == ["a_s00"]  # неизвестный сегмент отброшен
    body = mock_api["body"]
    assert body["model"] == "claude-opus-5"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"]["effort"] == "medium"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in mock_api["headers"]["anthropic-beta"]
    assert "a_s00" in body["messages"][0]["content"]


async def test_service_falls_back_to_heuristic(monkeypatch, tmp_path, media):
    import shutil

    from reels.config import Settings
    from reels.orchestrator.service import ReelsService
    from reels.storage.db import Database

    monkeypatch.setattr(llm, "available", lambda: True)

    async def boom(**kwargs):
        raise llm.LLMError("down")

    monkeypatch.setattr(llm, "parse", boom)
    settings = Settings(data_dir=tmp_path / "d", music_dir=tmp_path / "none")
    settings.data_dir.mkdir()
    db = Database(settings.db_url)
    await db.init()
    svc = ReelsService(db, settings)
    p = await svc.new_project(1)
    copy = tmp_path / "v.mp4"
    shutil.copy2(media["short"], copy)
    await svc.add_asset(p.id, "video", copy)
    board = await svc.plan(p.id)
    assert board.text.startswith("📋")
    await db.close()


async def test_service_uses_llm_plan(monkeypatch, tmp_path, media):
    import shutil

    from reels.config import Settings
    from reels.orchestrator.service import ReelsService
    from reels.storage.db import Database

    monkeypatch.setattr(llm, "available", lambda: True)
    calls = []

    async def fake_parse(**kwargs):
        calls.append(kwargs["output_format"].__name__)
        if kwargs["output_format"] is PlanDraft:
            seg = next(line for line in kwargs["content"].splitlines() if '"segment_id"' in line)
            sid = json.loads(seg)["segment_id"]
            return PlanDraft(template_id="listicle", title="t", hook_text="ЛЛМ-хук",
                             scenes=[PlanScene(segment_id=sid, role="hook", duration=3, text="", keep_audio=False)],
                             cta_text="", caption="", hashtags=[], color="warm")
        raise llm.LLMError("no captions in test")

    monkeypatch.setattr(llm, "parse", fake_parse)
    settings = Settings(data_dir=tmp_path / "d", music_dir=tmp_path / "none")
    settings.data_dir.mkdir()
    db = Database(settings.db_url)
    await db.init()
    svc = ReelsService(db, settings)
    p = await svc.new_project(1)
    copy = tmp_path / "v.mp4"
    shutil.copy2(media["short"], copy)
    await svc.add_asset(p.id, "video", copy)
    board = await svc.plan(p.id)
    assert "ЛЛМ-хук" in board.text and "Топ-N" in board.text
    assert "PlanDraft" in calls
    await db.close()
