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

*(To be completed in Milestone 2+ — one entry per fixed bug, one commit per fix.)*
