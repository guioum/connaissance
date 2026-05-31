"""Fixtures partagées.

Les tests des composants `core/` ne doivent pas dépendre de la présence d'une
vraie base ``~/Connaissance/``. ``TrackingDB`` exige normalement cette racine
(``require_connaissance_root``) — on la neutralise pour pointer une DB tmp.
"""
import pytest


@pytest.fixture
def tracking_db(tmp_path, monkeypatch):
    """``TrackingDB`` sur une base SQLite jetable, sans prérequis de racine."""
    from connaissance.core import tracking
    monkeypatch.setattr(tracking, "require_connaissance_root", lambda: None)
    db = tracking.TrackingDB(db_path=tmp_path / "tracking.db")
    try:
        yield db
    finally:
        db.close()
