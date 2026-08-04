from __future__ import annotations

import datetime as dt
import io
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from daily_kotoba import cache as cache_module


def test_healthz(client: TestClient):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["words"] == 10
    assert body["levels"] == ["N5", "N4", "N3", "N2", "N1"]
    assert body["today"]
    assert body["tz"] is not None or body["tz"] is None  # present key either way
    assert body["selected_word_id"] is None


def test_daily_json_shape(client: TestClient):
    r = client.get("/v1/daily.json")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"date", "word", "image"}
    word = body["word"]
    assert set(word.keys()) == {"id", "seq", "surface", "reading", "gloss", "pos", "jlpt"}
    assert body["image"]["url"] == "/v1/daily.png?w=760&h=300"
    assert body["image"]["width"] == 760
    assert body["image"]["height"] == 300


def test_daily_png_is_1bit_correct_size(client: TestClient):
    r = client.get("/v1/daily.png?w=400&h=200")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    img = Image.open(io.BytesIO(r.content))
    assert img.mode == "1"
    assert img.size == (400, 200)


def test_daily_png_default_size(client: TestClient):
    r = client.get("/v1/daily.png")
    img = Image.open(io.BytesIO(r.content))
    assert img.size == (760, 300)


def test_cache_hit_avoids_rerender(client: TestClient, monkeypatch):
    calls = []
    original = cache_module.render_card

    def spy(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(cache_module, "render_card", spy)

    r1 = client.get("/v1/daily.png?w=300&h=150")
    assert r1.status_code == 200
    assert len(calls) == 1

    r2 = client.get("/v1/daily.png?w=300&h=150")
    assert r2.status_code == 200
    assert len(calls) == 1  # second request was a cache/file-read hit, no re-render

    assert r1.content == r2.content
    assert r1.headers["etag"] == r2.headers["etag"]


def test_etag_conditional_get_returns_304(client: TestClient):
    r1 = client.get("/v1/daily.png?w=300&h=150")
    etag = r1.headers["etag"]

    r2 = client.get("/v1/daily.png?w=300&h=150", headers={"If-None-Match": etag})
    assert r2.status_code == 304
    assert r2.content == b""


def test_size_guard_too_large(client: TestClient):
    r = client.get("/v1/daily.png?w=9999&h=300")
    assert r.status_code == 422


def test_size_guard_too_small(client: TestClient):
    r = client.get("/v1/daily.png?w=95&h=300")
    assert r.status_code == 422


def test_history_endpoint(client: TestClient, set_today):
    for day in [dt.date(2026, 3, 1), dt.date(2026, 3, 2), dt.date(2026, 3, 3)]:
        set_today(day)
        r = client.get("/v1/daily.json")
        assert r.status_code == 200

    r = client.get("/v1/history?limit=2")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert body[0]["date"] == "2026-03-03"
    assert body[1]["date"] == "2026-03-02"


def test_history_limit_guard(client: TestClient):
    r = client.get("/v1/history?limit=0")
    assert r.status_code == 422
    r = client.get("/v1/history?limit=101")
    assert r.status_code == 422


def test_healthz_never_creates_selection(client: TestClient):
    for _ in range(3):
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["selected_word_id"] is None


def test_daily_png_rejects_unknown_title_style(client):
    # A closed enum means bad input is a clean 422 rather than an arbitrary string
    # reaching the renderer and minting a cache entry.
    assert client.get("/v1/daily.png?title=../../etc/passwd").status_code == 422
    assert client.get("/v1/daily.png?title=japanese").status_code == 422


def test_title_cache_entries_are_bounded(client, seeded_db):
    # Whatever a caller does, the number of distinct cache files per (day, size) is
    # capped by the enum — this is what stops title values exhausting the disk.
    from daily_kotoba.render import TitleStyle

    for style in TitleStyle:
        assert client.get(f"/v1/daily.png?w=370&h=233&title={style.value}").status_code == 200
    for bogus in ("x", "y", "z"):
        assert client.get(f"/v1/daily.png?w=370&h=233&title={bogus}").status_code == 422

    cache_root = Path(seeded_db.cache_dir)
    pngs = list(cache_root.rglob("370x233*.png"))
    assert len(pngs) == len(TitleStyle)
