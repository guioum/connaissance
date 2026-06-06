"""Migration de schéma additive (tracking._migrate) sur une base ancienne.

Garantit qu'ouvrir une base créée par une version antérieure ajoute les
colonnes manquantes (notamment celles de ``doc_classification``, dont l'INSERT
est généré dynamiquement depuis ``_CLS_COLS``) au lieu de planter.
"""
import sqlite3

import pytest


def _make_old_db(path):
    """Base « ancienne » : files sans `size`, doc_classification amputée de
    colonnes qu'une version ultérieure ajouterait (sujet/reasons/model)."""
    c = sqlite3.connect(path)
    c.executescript(
        """
        CREATE TABLE files (
            id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL UNIQUE,
            file_type TEXT NOT NULL, source_type TEXT, source_path TEXT,
            entity_type TEXT, entity_slug TEXT, created TEXT, modified TEXT,
            message_id TEXT, hash TEXT, mtime REAL, updated_at TEXT);
        CREATE TABLE doc_classification (
            rel_path TEXT NOT NULL UNIQUE, hash TEXT, entity TEXT,
            entity_type TEXT, entity_slug TEXT, category TEXT, date TEXT,
            title TEXT, confidence TEXT, status TEXT, size INTEGER, mtime REAL,
            classified_at TEXT NOT NULL
                DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
            updated_at TEXT NOT NULL
                DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')));
        """
    )
    c.commit()
    c.close()


@pytest.fixture
def _no_root(monkeypatch):
    from connaissance.core import tracking
    monkeypatch.setattr(tracking, "require_connaissance_root", lambda: None)
    return tracking


def test_migration_ajoute_colonnes_manquantes(_no_root, tmp_path):
    tracking = _no_root
    dbp = tmp_path / "old.db"
    _make_old_db(dbp)

    cols_avant = {r[1] for r in sqlite3.connect(dbp).execute(
        "PRAGMA table_info(doc_classification)")}
    assert "sujet" not in cols_avant  # base réellement ancienne

    db = tracking.TrackingDB(db_path=dbp)
    try:
        info = db._conn.execute(
            "PRAGMA table_info(doc_classification)").fetchall()
        cols = {r[1] for r in info}
        # Toutes les colonnes de la fiche sont présentes après migration.
        assert set(tracking.TrackingDB._CLS_COLS) <= cols
        # Types corrects sur les colonnes non-TEXT.
        types = {r[1]: r[2] for r in info}
        assert types["size"] == "INTEGER"
        assert types["mtime"] == "REAL"
        # files.size ajouté aussi.
        fcols = {r[1] for r in db._conn.execute("PRAGMA table_info(files)")}
        assert "size" in fcols
        # L'INSERT dynamique fonctionne (aurait planté sans migration).
        db.upsert_classification(
            "a/b.pdf",
            {"entity": "x", "sujet": "impots", "reasons": ["r1"],
             "size": 10, "mtime": 1.0, "status": "auto"})
        row = db.get_classification("a/b.pdf")
        assert row["sujet"] == "impots"
    finally:
        db.close()


def test_migration_idempotente(_no_root, tmp_path):
    tracking = _no_root
    dbp = tmp_path / "old.db"
    _make_old_db(dbp)
    db = tracking.TrackingDB(db_path=dbp)
    try:
        db._migrate()  # second passage : ne doit pas lever
        db._migrate()
    finally:
        db.close()


def test_index_doc_classification_crees(_no_root, tmp_path):
    tracking = _no_root
    dbp = tmp_path / "old.db"
    _make_old_db(dbp)
    db = tracking.TrackingDB(db_path=dbp)
    try:
        idx = {r[0] for r in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_doc_cls_status" in idx
        assert "idx_doc_cls_entity" in idx
        assert "idx_files_size" in idx
    finally:
        db.close()
