# daily-kotoba

Serves one Japanese word a day to an ESPHome e-paper display (Seeed reTerminal
E1001: ESP32-S3, 8 MB PSRAM, 7.5" 800×480 mono).

## Why images?

The display's firmware can't carry full CJK glyph coverage — there's no reasonable
way to flash every kanji into an ESP32. So `daily-kotoba` renders the word card
server-side to a **1-bit PNG** and the device just blits it via ESPHome's
`online_image` component (`format: PNG`, `type: BINARY`). The device keeps its
embedded Latin font only for its own chrome — date, time, calendar.

This is a small, personal, single-user service: no auth, no multi-tenancy, no
scheduler, no metrics stack.

## Quickstart

```bash
docker compose up -d
curl http://localhost:8000/v1/daily.json
curl http://localhost:8000/v1/daily.png?w=760&h=300 -o card.png
```

`docker-compose.yml` pulls the pre-built image from GHCR — nothing is built locally.

## Configuration

All settings are environment variables, prefix `KOTOBA_`:

| env | default | meaning |
|---|---|---|
| `KOTOBA_DB_PATH` | `/data/kotoba.db` | live DB (writable volume) |
| `KOTOBA_SEED_DB` | `/app/seed/kotoba-seed.db` | baked read-only seed, copied in on first boot |
| `KOTOBA_CACHE_DIR` | `/data/cache` | rendered PNG cache |
| `KOTOBA_CACHE_KEEP_DAYS` | `7` | prune horizon for the PNG cache |
| `KOTOBA_LEVELS` | `N5,N4,N3,N2,N1` | candidate JLPT pool, comma-separated |
| `KOTOBA_FONT_DIR` | `/app/fonts` | Noto Sans JP location |
| `KOTOBA_LOG_LEVEL` | `INFO` | |
| `TZ` | `Australia/Sydney` (Dockerfile default) | standard Docker TZ; sets the day boundary |

## Endpoints

| method | path | notes |
|---|---|---|
| `GET` | `/healthz` | read-only status; never mints the day's word |
| `GET` | `/v1/daily.json` | today's word + image URL |
| `GET` | `/v1/daily.png?w=&h=&title=&polarity=` | the 1-bit word card (`w`: 96–800, `h`: 48–480) |
| `GET` | `/v1/history?limit=` | last N selections, newest first (1–100, default 30) |

`title` (`none` \| `ja` \| `en`) draws a heading opposite the JLPT badge. `ja`
renders 日本語 into the image, which is the only way to get it onto a display whose
firmware has no CJK glyphs.

`polarity` (`positive` \| `mask`) chooses which pixel value is ink. `positive` is
black ink on white paper. `mask` is the exact complement, for clients that read a
1-bit image as an alpha mask rather than as a picture — see the ESPHome note below.
Both are closed enums so the per-day cache stays bounded.

Example card at the default 760×300:

```
+--------------------------------------------+
|                                    [ N4 ]  |
|              た    の                       |
|            楽 し い                          |
|                                            |
|     fun, enjoyable, pleasant               |
|            (i-adjective)                   |
+--------------------------------------------+
```

## How the word is picked

The first request of the day that needs a word materialises it — no cron, no
scheduler. Never-shown words go first, then the pool recycles from the
longest-unshown word onward. See `src/daily_kotoba/selection.py`.

## ESPHome

A working snippet lives in `esphome/daily-kotoba.yaml`. It replaces the
`http_request` + `json::parse_json` block you'd otherwise need for the word with a
single `online_image`. Two things worth knowing:

- `online_image` keeps the decoded image in RAM for the display's lifetime — on an
  ESP32-S3 that's why PSRAM (octal mode) matters here.
- The `on_error` handler deliberately leaves the previous card on screen rather than
  blanking it, so a transient network hiccup doesn't wipe today's word.
- **Drawing from a `display:` lambda wants `polarity=positive`** (the default), with
  `it.image(x, y, id(card), COLOR_OFF, COLOR_ON)`. The swapped arguments are not a
  typo: ESPHome's BINARY decoder sets a bit for *bright* pixels, so without the swap
  the paper is inked and the glyphs are left blank.
- **Drawing from LVGL wants `polarity=mask`.** LVGL receives a BINARY image as
  `LV_COLOR_FORMAT_A1` — alpha only — and an image widget has no equivalent of those
  two colour arguments, so the polarity has to be right on arrival. Pair it with
  `image_recolor` set to whatever your page uses for ink.

## Attribution

See [NOTICE](NOTICE) for full attribution. In short: word data derives from JMdict
and a community JLPT vocabulary list, both CC BY-SA 4.0; the bundled
`kotoba-seed.db` is therefore also CC BY-SA 4.0. The service code itself is MIT.
Noto Sans JP is SIL OFL 1.1.

## License

Code: MIT, see [LICENSE](LICENSE).
