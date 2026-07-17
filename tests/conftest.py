"""Fixtures partagées.

Les tests des composants `core/` ne doivent pas dépendre de la présence d'une
vraie base ``~/Connaissance/``. ``TrackingDB`` exige normalement cette racine
(``require_connaissance_root``) — on la neutralise pour pointer une DB tmp.
"""
import pytest


@pytest.fixture(autouse=True)
def _isole_config_reelle(tmp_path, monkeypatch):
    """Garde-fou : aucun test ne doit écrire dans le vrai ``~/Connaissance``.

    Les constantes de ``tracking``/``ledger`` sont figées à l'import sur la
    vraie racine ; sans cette fixture, ``snapshot_db()`` (appelé par
    ``classify.apply``) VACUUM la vraie ``tracking.db`` dans le vrai
    ``.config/backups/`` (et sa rotation ``keep=10`` éjecte les vrais
    backups), et ``safe_move`` journalise dans le vrai
    ``.config/journal/ledger/`` — pollution rejouée ensuite par
    ``audit restore-journals``. Constaté en réel le 2026-07-17.

    ``ledger.py`` lie ``LEDGER_JOURNAL_DIR`` par valeur à l'import : il faut
    patcher le nom dans **les deux** modules.
    """
    from connaissance.core import ledger, paths, tracking

    cfg = tmp_path / "_config_isole"
    journal = cfg / "journal"
    monkeypatch.setattr(tracking, "DB_PATH", cfg / "tracking.db")
    monkeypatch.setattr(tracking, "BACKUPS_DIR", cfg / "backups")
    monkeypatch.setattr(tracking, "JOURNAL_DIR", journal)
    monkeypatch.setattr(tracking, "LEDGER_JOURNAL_DIR", journal / "ledger")
    monkeypatch.setattr(tracking, "USAGE_JOURNAL", journal / "llm_usage.jsonl")
    monkeypatch.setattr(ledger, "LEDGER_JOURNAL_DIR", journal / "ledger")

    # VIEWS_ROOT est lui aussi importé par valeur (sujets, documents,
    # snapshots) : sans patch, les vues de test atterrissent dans le vrai
    # ~/Connaissance/Vues/ (symlink pytest constaté en réel le 2026-07-17).
    # Cache d'aliases (résolution d'entités) : jamais partagé entre tests.
    from connaissance.core import resolution
    resolution.invalidate_alias_cache()

    vues = tmp_path / "_vues_isole"
    monkeypatch.setattr(paths, "VIEWS_ROOT", vues)
    monkeypatch.setattr(paths, "SNAPSHOTS_DIR", cfg / "snapshots")
    for module in ("sujets", "documents", "snapshots", "ledger"):
        mod = __import__(f"connaissance.commands.{module}",
                         fromlist=[module])
        monkeypatch.setattr(mod, "VIEWS_ROOT", vues, raising=False)
        monkeypatch.setattr(mod, "SNAPSHOTS_DIR", cfg / "snapshots",
                            raising=False)


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
