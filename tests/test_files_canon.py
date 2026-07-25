"""Convention canonique de files.path (NFC, relatif au home) : canon,
frontières, migration auto-réparante — le mélange absolu/relatif rendait
move_file inopérant (10 308 chemins périmés après le grand déplacement)."""
import sqlite3
import unicodedata

from connaissance.core import tracking
from connaissance.core.tracking import canon_file_path


def test_canon_file_path_forms(monkeypatch, tmp_path):
    monkeypatch.setattr(tracking, "BASE_PATH", tmp_path)
    nfd = unicodedata.normalize("NFD", "Téléchargés")
    # absolu sous home → relatif NFC
    assert canon_file_path(tmp_path / nfd / "a.pdf") == "Téléchargés/a.pdf"
    assert canon_file_path(str(tmp_path / "Documents" / "x.pdf")) == \
        "Documents/x.pdf"
    # legacy relatif à ~/Connaissance → re-préfixé
    assert canon_file_path("Transcriptions/Documents/a.md") == \
        "Connaissance/Transcriptions/Documents/a.md"
    # déjà canonique → inchangé ; hors home → absolu NFC
    assert canon_file_path("Documents/x.pdf") == "Documents/x.pdf"
    assert canon_file_path("/Volumes/SSD/x.pdf") == "/Volumes/SSD/x.pdf"


def test_move_file_matches_across_forms(monkeypatch, tmp_path, tracking_db):
    """Un move passé en ABSOLU NFD doit matcher une ligne enregistrée en
    relatif NFC — le cas exact du grand déplacement."""
    monkeypatch.setattr(tracking, "BASE_PATH", tmp_path)
    # enregistré en LEGACY (relatif à ~/Connaissance, comme register_document)
    tracking_db.register_file("Transcriptions/Documents/Ét é/a.md",
                              "transcription")
    # déplacé en ABSOLU NFD (comme le ledger/relocate)
    nfd = unicodedata.normalize("NFD", "Ét é")
    old_abs = tmp_path / "Connaissance" / "Transcriptions" / "Documents" / nfd / "a.md"
    new_abs = tmp_path / "Connaissance" / "Transcriptions" / "Documents" / "bn" / "b.md"
    tracking_db.move_file(old_abs, new_abs)
    assert tracking_db.get_file(
        "Connaissance/Transcriptions/Documents/bn/b.md") is not None
    assert tracking_db.get_file("Transcriptions/Documents/Ét é/a.md") is None


def test_migration_normalizes_existing_rows(monkeypatch, tmp_path):
    """Une base héritée (mélange absolu/relatif, doublon des deux formes) est
    normalisée à l'ouverture ; le doublon legacy est absorbé."""
    monkeypatch.setattr(tracking, "require_connaissance_root", lambda: None)
    monkeypatch.setattr(tracking, "BASE_PATH", tmp_path)
    db_path = tmp_path / "t.db"
    db = tracking.TrackingDB(db_path=db_path)
    db.close()
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO files (path, file_type) VALUES (?, 'source')",
                 (str(tmp_path / "Documents" / "a.pdf"),))       # absolu
    conn.execute("INSERT INTO files (path, file_type, hash) VALUES "
                 "(?, 'transcription', 'sha-x')",
                 ("Transcriptions/Documents/a.md",))              # legacy rel
    conn.execute("INSERT INTO files (path, file_type) VALUES "
                 "(?, 'transcription')",
                 ("Connaissance/Transcriptions/Documents/a.md",))  # déjà canon
    conn.commit(); conn.close()

    db = tracking.TrackingDB(db_path=db_path)                     # → _migrate
    rows = {r[0] for r in db._conn.execute("SELECT path FROM files")}
    db.close()
    assert rows == {"Documents/a.pdf",
                    "Connaissance/Transcriptions/Documents/a.md"}


def test_missing_resumes_api_shape(monkeypatch, tmp_path, tracking_db):
    """missing_resumes rend des chemins relatifs à ~/Connaissance (API des
    appelants), quel que soit le stockage canonique."""
    monkeypatch.setattr(tracking, "BASE_PATH", tmp_path)
    tracking_db.register_file(
        str(tmp_path / "Connaissance/Transcriptions/Documents/x.md"),
        "transcription", source_type="document")
    rows = tracking_db.missing_resumes()
    assert [r["path"] for r in rows] == ["Transcriptions/Documents/x.md"]
