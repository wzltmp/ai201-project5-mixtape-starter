# Project 5: Mixtape Bug Hunt — Submission

## Milestone 1 — Codebase Map

### How the app is put together

Mixtape is a Flask JSON API with a three-layer structure: **routes** (HTTP), **services** (business logic), **models** (persistence). There is no frontend — every feature is an endpoint returning JSON.

#### `app.py` — application factory

Creates the Flask app, configures SQLite (`mixtape.db` by default, overridable via `DATABASE_URL` / a config dict for tests), and registers four blueprints with URL prefixes: `/songs`, `/playlists`, `/users`, `/feed`. The shared `db = SQLAlchemy()` object lives *here*, and `models.py` imports it back from `app` — a circular arrangement that works under `FLASK_APP=app:create_app flask run` but breaks under `python app.py` (the module gets imported twice, once as `__main__` and once as `app`, producing two different `db` objects). `db.create_all()` runs inside the factory, so tables always exist.

#### `models.py` — 7 models + 3 association tables

All primary keys are UUID strings (`String(36)`), not integers. All timestamp defaults are timezone-aware UTC (`datetime.now(timezone.utc)`) — but SQLite hands them back *naive*, which is why some services defensively re-attach `tzinfo` (see `streak_service.py:64-65`).

| Model | Role | Notable details |
|---|---|---|
| `User` | account + streak state | `listening_streak`, `last_listened_at`; self-referential many-to-many `friends` (symmetric — seed inserts both directions), `lazy="dynamic"` |
| `Song` | a shared track | `shared_by` FK = the user who shared it (drives notifications); `tags` many-to-many, `lazy="subquery"` (always eager-loaded) |
| `Tag` | genre label | unique name |
| `ListeningEvent` | user × song × timestamp | the raw material for both the feed and streaks |
| `Rating` | user's 1–5 score | `UniqueConstraint(user_id, song_id)` — one rating per user per song; re-rating updates in place |
| `Playlist` | collaborative list | songs many-to-many **through `playlist_entries`**, which carries extra columns: `position` (explicit ordering — not insertion order), `added_by`, `added_at`. `position` and `added_by` are `nullable=False` |
| `Notification` | inbox item | `user_id` recipient, free-form `notification_type` string, `read` flag |

Every model has a `to_dict()` serializer; routes return `jsonify(...)` of these dicts.

#### `routes/` — thin HTTP layer

One blueprint per resource. Each route does exactly three things: parse input (query params / JSON body), call **one** service function, and translate errors — services raise `ValueError`, routes catch it and return 400/404 JSON. No business logic lives here. (One small exception: `routes/users.py:get_user` queries the `User` model directly instead of going through a service.)

- `songs.py` — `GET /songs/search?q=`, `GET /songs/<id>`, `POST /songs/<id>/rate`, `POST /songs/<id>/listen`
- `playlists.py` — `POST /playlists/`, `GET /playlists/<id>`, `GET+POST /playlists/<id>/songs`
- `users.py` — `GET /users/<id>`, `GET /users/<id>/streak`, `GET /users/<id>/notifications`, `POST /users/notifications/<id>/read`
- `feed.py` — `GET /feed/<user_id>/listening-now`, `GET /feed/<user_id>/activity`

#### `services/` — all business logic (and all five bugs, per the README)

- `streak_service.py` — `record_listening_event()` creates a `ListeningEvent` then calls `update_listening_streak()`, which compares calendar days between `last_listened_at` and now: same day → no change, consecutive day → +1, gap → reset to 1.
- `feed_service.py` — `get_friends_listening_now()` pulls friends' `ListeningEvent`s newer than a module-level `RECENT_THRESHOLD` (currently 24 h), dedupes to the most recent song per friend. `get_activity_feed()` is the deliberately-unfiltered variant (latest N events regardless of age).
- `search_service.py` — `search_songs()` does a case-insensitive `ILIKE` on title/artist, with an `outerjoin` to `song_tags`.
- `notification_service.py` — `create_notification()` is the shared write path. `add_to_playlist()` both mutates the playlist *and* notifies the song's sharer. `rate_song()` upserts a `Rating`. `get_notifications()` / `mark_as_read()` are the read side.
- `playlist_service.py` — playlist CRUD; `get_playlist_songs()` joins through `playlist_entries` ordered by `position`.

#### `tests/` and `seed_data.py`

Tests use the app factory with in-memory SQLite, so they run against a clean DB without touching `mixtape.db`. **Baseline: 3 of 13 tests fail on the untouched starter** (`test_playlist_returns_all_songs`, `test_playlist_returns_songs_in_order`, `test_streak_increments_on_sunday`) — the suite already encodes the expected behavior for two of the open issues. `seed_data.py` drops and recreates everything: 5 users with friendships centered on nova, 13 songs in a deliberate 0/1/3-tag mix, 3 playlists of 7 songs each, listening events both recent (≤30 min) and old (2 h–14 days), and one sample `song_added_to_playlist` notification showing the correct notification pattern.

### Data flow #1 (featured): friend adds your song to a playlist → you get notified

1. `POST /playlists/<playlist_id>/songs` with `{"song_id", "added_by"}` → `routes/playlists.py:add_song()`
2. → `notification_service.add_to_playlist(playlist_id, song_id, added_by)` — validates that song, adder, and playlist all exist (`ValueError` → 400 if not)
3. → if the song isn't already in the playlist: `playlist.songs.append(song)` + commit
4. → if `song.shared_by != added_by`: `create_notification(user_id=song.shared_by, type="song_added_to_playlist", body="<adder> added your song '<title>' to the playlist '<name>'.")`
5. → the sharer sees it via `GET /users/<their_id>/notifications`

Two things I noticed tracing this **in the running app**, not just on paper:

- **Layering oddity:** the playlist mutation lives in `notification_service`, not `playlist_service`. The service boundary here is "notification-generating actions," not "playlist actions."
- **The happy path doesn't actually survive contact:** step 3 inserts into `playlist_entries` via the plain relationship, which supplies neither `position` nor `added_by` — both `NOT NULL`. A live `POST` returns **500** with `sqlite3.IntegrityError: NOT NULL constraint failed: playlist_entries.position` (commit rolls back, nothing persists). This is *not* one of the five listed issues, but it explains why the seed script inserts playlist entries with raw `playlist_entries.insert().values(...)` instead of the relationship.

### Data flow #2 (brief): listening updates your streak

`POST /songs/<id>/listen` → `routes/songs.py:listen()` → `streak_service.record_listening_event()` → inserts a `ListeningEvent`, then `update_listening_streak(user, now)` compares `now.date()` against `last_listened_at.date()` (re-attaching UTC tzinfo, since SQLite returns naive datetimes) and applies the same-day / consecutive-day / gap rules → single commit covers both the event and the user row.

### Patterns I noticed

- **App factory + blueprints**, with the `db` object owned by `app.py` and imported everywhere else — the source of the `python app.py` double-import trap.
- **Consistent error convention:** services raise `ValueError`, routes translate to 4xx JSON. No other exception types are used, so anything unexpected surfaces as a 500.
- **UUID string PKs and `to_dict()` serializers** on every model; routes never hand-build response dicts.
- **Datetime discipline is asymmetric:** writes are timezone-aware UTC, reads from SQLite are naive; only `streak_service` compensates.
- **Eager vs lazy loading is deliberate:** `Song.tags` and `Playlist.songs` are `lazy="subquery"`; `User.friends` is `lazy="dynamic"` (query object, supports further filtering).

---

## The five issues — first read and plan

Hypotheses formed while reading (to be confirmed by reproduction before any fix, per M2):

| # | Issue | Service | Initial suspicion |
|---|---|---|---|
| 1 | Streak keeps resetting | `streak_service.py` | `update_listening_streak` increments only when `today.weekday() != 6` — a consecutive-day listen that lands on a **Sunday** falls through to the reset branch. The repo's own `test_streak_increments_on_sunday` fails on the starter. |
| 2 | "Friends Listening Now" shows people from yesterday | `feed_service.py` | `RECENT_THRESHOLD = timedelta(hours=24)` — a 24-hour window is "listened in the past day," not "listening now." Seed data pointedly creates events at ≤30 min and at 2 h+. |
| 3 | Same song shows up twice in search | `search_service.py` | The `outerjoin` to `song_tags` fans out one row per tag (verified: 3 raw SQL rows for a 3-tag song). Interestingly the duplicates are currently **masked** — legacy `Query.all()` dedupes entities — so reproduction needs SQL-level evidence. |
| 4 | Notified on playlist-add but not on rating | `notification_service.py` | `add_to_playlist()` calls `create_notification()`; `rate_song()` never does. Missing notification write, same guard needed (don't notify when rating your own song). |
| 5 | Last song in a playlist never shows up | `playlist_service.py` | `get_playlist_songs` returns `songs[:-1]` — the slice unconditionally drops the final element. Two starter tests fail because of it. |

**Plan — first three:** **#5** (playlist off-by-one), **#1** (streak Sunday condition), **#4** (missing rating notification). All three have unambiguous root causes and either already-failing tests (#1, #5) or a trivially reproducible absence (#4). **Stretch:** #2 (feed threshold — needs the issue description to pin the intended window) and #3 (search fan-out — needs care because the symptom is version-masked).

---

## Root Cause Analyses

*(M2 status: reproduction fields completed for the three chosen bugs. Root cause / fix / verification fields land with each fix commit in M3.)*

### Issue #1 — My listening streak keeps resetting

**The issue as reported:** Users' listening streaks reset even when they listen on consecutive days.

**How I reproduced it:** The buggy branch only executes when *today* is a Sunday, so it can't be triggered through the live API on an arbitrary day (I did this milestone on a Thursday) — the app state needed is a `last_listened_at` of yesterday *and* a current date falling on Sunday. I reproduced it by calling `update_listening_streak()` directly with controlled `now` values against an in-memory DB, with a control case to isolate the condition:

```
tue_to_wed: Tue 2026-06-30 streak=1 -> Wed 2026-07-01 streak=2 (expected 2, ok)
sat_to_sun: Sat 2026-06-27 streak=1 -> Sun 2026-06-28 streak=1 (expected 2, BUG)
```

Consecutive weekday listens increment correctly; the identical sequence crossing Saturday→Sunday resets to 1. This matches the "keeps resetting" phrasing — it silently eats the streak once a week. The starter suite already encodes the expectation: `tests/test_streaks.py::test_streak_increments_on_sunday` fails on the untouched repo with `assert 1 == 2`.

**Root cause:** *(M3)*

**The fix:** *(M3)*

**How I verified it:** *(M3)*

### Issue #4 — Notified on playlist-add but not on rating

**The issue as reported:** A user got a notification when a friend added their shared song to a playlist, but not when a friend rated their song.

**How I reproduced it:** Live API, seeded DB. Baseline: `GET /users/<nova>/notifications` returns `count: 1` — the seeded `song_added_to_playlist` notification, proving the notification pipeline works for playlist adds. Then darius rates nova's shared song:

```
POST /songs/155016c2…/rate  {"user_id": "<darius>", "score": 5}   → HTTP 201, Rating persisted
GET  /users/<nova>/notifications                                   → count: 1, types: ['song_added_to_playlist']
```

The rating succeeds and is stored, but nova's notification list is unchanged — no `song_rated` entry appears. Action works, side effect is absent.

**Root cause:** *(M3)*

**The fix:** *(M3)*

**How I verified it:** *(M3)*

### Issue #5 — The last song in a playlist never shows up

**The issue as reported:** Whatever song is last in a playlist is missing from the playlist view.

**How I reproduced it:** Compared DB ground truth against the API response for the seeded playlist "Late Night Vibes". Direct query of `playlist_entries` shows **7** entries, positions 1–7, with "Free Throws" at position 7. The endpoint:

```
GET /playlists/9e3c0f65…/songs → count: 6
titles: [Midnight Drive, Still Waters, First Light, Block Party, Late Night Session, Golden Hour]
```

Exactly the position-7 song ("Free Throws") is missing; the other six come back in position order. Reproduces on every playlist regardless of size. The starter suite also encodes it: `tests/test_playlists.py::test_playlist_returns_all_songs` (gets 4, expects 5) and `test_playlist_returns_songs_in_order` both fail on the untouched repo.

**How I found the root cause:** Started from the symptom's endpoint: `GET /playlists/<id>/songs` → `routes/playlists.py:get_songs()`, which only calls `playlist_service.get_playlist_songs()` and reports `len()` of whatever comes back — so the loss had to be in the service. Read `get_playlist_songs()` top-down: the query (join `Song` ↔ `playlist_entries`, filter by playlist, `order_by(asc(position))`) is correct — I verified that by running the same query directly and getting all 7 rows. The confidence moment was the return line: `[song.to_dict() for song in songs[:-1]]`. The `[:-1]` slice explains the evidence exactly — DB says 7, API says 6, and the missing song is always the *highest position*, because the list is sorted ascending by position before the slice cuts the tail.

**The root cause:** Python's `list[:-1]` slice means "everything except the last element." `get_playlist_songs()` built the correct, position-ordered list of songs and then returned `songs[:-1]`, unconditionally discarding the final element. Since the list is sorted ascending by `playlist_entries.position`, the discarded element is always the most recently positioned song — hence "the last song in a playlist never shows up," for every playlist, every time. (Likely a leftover from debugging or a mistaken attempt to trim something; the function's own docstring says "returns all songs in the playlist.")

**My fix and side-effect check:** Changed the return to `[song.to_dict() for song in songs]` — removing only the slice (`services/playlist_service.py:66`). The query and ordering logic were already correct, so nothing else needed to change. Side-effect checks: (1) the seeded 7-song playlist now returns `count: 7` with "Free Throws" present and titles still in position order; (2) boundary both sides — a 1-song playlist returns 1 song (previously 0 — the old code's worst case) and an empty playlist still returns `[]` (unaffected before and after, since `[][:-1] == []`); (3) `get_playlist_songs`'s only caller is `routes/playlists.py:get_songs`, so no other feature consumes this list; (4) full test suite: both previously-failing playlist tests now pass, and nothing else regressed (went from 3 failed/10 passed to 1 failed/12 passed — the remaining failure is Issue #1's Sunday test, fixed next).

### Stretch bugs — reproduction notes (#2, #3)

Captured while in reproduction mode, ahead of the stretch fixes:

- **#2 (feed shows people from yesterday):** `GET /feed/<kenji>/listening-now` returns nova "listening now" a song she actually played **2.3 hours earlier** (seeded event). aaliya's 34-hour-old event is excluded by the current 24 h cutoff — so the window works, it's just far too wide to mean "now".
- **#3 (duplicate search results):** the search query's `outerjoin` to `song_tags` fans out at the SQL level — for the 3-tag song "Crown Heights Anthem", `query.count()` = **3 raw rows** while `.all()` returns **1 entity**. The duplicates are currently masked by SQLAlchemy's legacy `Query` entity-deduplication (2.0.51), so the live API shows `count: 1`; on any code path without that dedup (2.0-style `select()`, counts, pagination) the same query triples the song. Reproduction is SQL-level by necessity.
