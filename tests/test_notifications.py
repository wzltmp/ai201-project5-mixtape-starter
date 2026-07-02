"""
tests/test_notifications.py — Mixtape

Regression tests for Issue #4: rating a friend's shared song must
notify the sharer. rate_song() originally persisted the Rating but
never called create_notification(), so these tests fail against the
pre-fix code and pass after it.
"""

import pytest
from app import create_app, db
from models import User, Song, Notification
from services.notification_service import rate_song


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def sharer_and_song(app):
    """A user who shared a song, plus a friend who will rate it."""
    with app.app_context():
        sharer = User(username="sharer", email="sharer@example.com")
        rater = User(username="rater", email="rater@example.com")
        db.session.add_all([sharer, rater])
        db.session.flush()

        song = Song(title="Golden Hour", artist="Solange K", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()
        yield {"sharer": sharer, "rater": rater, "song": song}


def test_rating_a_friends_song_notifies_the_sharer(app, sharer_and_song):
    """
    When someone else rates a shared song, the sharer receives a
    'song_rated' notification naming the rater, the song, and the score.
    """
    with app.app_context():
        sharer = sharer_and_song["sharer"]
        rater = sharer_and_song["rater"]
        song = sharer_and_song["song"]

        rate_song(rater.id, song.id, 4)

        notifications = db.session.query(Notification).filter_by(
            user_id=sharer.id, notification_type="song_rated"
        ).all()
        assert len(notifications) == 1
        assert "rater" in notifications[0].body
        assert "Golden Hour" in notifications[0].body
        assert "4" in notifications[0].body


def test_rating_own_song_does_not_notify(app, sharer_and_song):
    """Rating your own shared song must not generate a self-notification."""
    with app.app_context():
        sharer = sharer_and_song["sharer"]
        song = sharer_and_song["song"]

        rate_song(sharer.id, song.id, 5)

        notifications = db.session.query(Notification).filter_by(
            user_id=sharer.id
        ).all()
        assert notifications == []
