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


def test_daily_png_polarity_is_part_of_the_cache_key(client, seeded_db):
    # The two polarities share a day and a size, so if polarity were left out of the
    # cache path the second request would be served the first one's bytes — and the
    # device would silently get a card with its ink and paper the wrong way round.
    positive = client.get("/v1/daily.png?w=370&h=233&polarity=positive")
    mask = client.get("/v1/daily.png?w=370&h=233&polarity=mask")
    assert positive.status_code == mask.status_code == 200
    assert positive.content != mask.content
    assert positive.headers["ETag"] != mask.headers["ETag"]

    # Order must not matter either: re-requesting the first must not now return the
    # second's cached bytes.
    assert client.get("/v1/daily.png?w=370&h=233&polarity=positive").content == positive.content

    assert len(list(Path(seeded_db.cache_dir).rglob("370x233*.png"))) == 2


def test_daily_png_defaults_to_positive(client):
    # The device's URL omits polarity today; the default must stay the human-facing
    # one, so an existing deployment cannot be flipped by adding the parameter.
    assert (
        client.get("/v1/daily.png?w=370&h=233").content
        == client.get("/v1/daily.png?w=370&h=233&polarity=positive").content
    )


def test_daily_png_rejects_unknown_polarity(client):
    assert client.get("/v1/daily.png?polarity=inverted").status_code == 422
    assert client.get("/v1/daily.png?polarity=1").status_code == 422


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


def test_cache_is_bounded_within_a_single_day(client, seeded_db):
    # w/h span ~305k combinations and _prune only drops *expired* days, so without a
    # per-day ceiling a client looping over sizes could fill the volume before the
    # day rolls over.
    from daily_kotoba.cache import MAX_ENTRIES_PER_DAY

    for i in range(MAX_ENTRIES_PER_DAY + 20):
        assert client.get(f"/v1/daily.png?w={200 + i}&h=200").status_code == 200

    day_dirs = [d for d in Path(seeded_db.cache_dir).iterdir() if d.is_dir()]
    assert len(day_dirs) == 1
    assert len(list(day_dirs[0].glob("*.png"))) <= MAX_ENTRIES_PER_DAY


def test_cache_eviction_keeps_the_most_recent_entries(client, seeded_db):
    from daily_kotoba.cache import MAX_ENTRIES_PER_DAY

    for i in range(MAX_ENTRIES_PER_DAY + 10):
        client.get(f"/v1/daily.png?w={300 + i}&h=200")

    day_dir = next(d for d in Path(seeded_db.cache_dir).iterdir() if d.is_dir())
    names = {f.name for f in day_dir.glob("*.png")}
    last = 300 + MAX_ENTRIES_PER_DAY + 9
    assert any(str(last) in n for n in names)  # newest survived
    assert not any(f"{300}x200-" in n for n in names)  # oldest evicted


def test_cache_read_survives_concurrent_eviction(client, seeded_db, monkeypatch):
    """A cache hit must not 500 when the entry is evicted mid-request.

    The window is real: _enforce_day_quota runs inside whichever request happens to
    be writing, so it can unlink an entry another request is already serving. This
    patches Path.read_bytes rather than any internal helper, so it fails against an
    implementation that checks existence and then reads.
    """
    client.get("/v1/daily.png?w=310&h=210")  # populate the entry

    real_read_bytes = Path.read_bytes
    fired = {"done": False}

    def vanishing_read(self):
        # First read of a cached PNG behaves as if a concurrent quota sweep just
        # unlinked it, which is precisely the TOCTOU window.
        if not fired["done"] and self.suffix == ".png":
            fired["done"] = True
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", vanishing_read)

    r = client.get("/v1/daily.png?w=310&h=210")
    assert fired["done"], "the simulated eviction never triggered"
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert len(r.content) > 0
    assert r.headers["ETag"] and r.headers["Last-Modified"]
