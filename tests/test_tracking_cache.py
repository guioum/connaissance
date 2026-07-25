"""Tests du cache JIT de tracking.db : hash, invalidation, read_path (SSD), simhash."""
import hashlib
import os

from connaissance.core.dedup import from_hex


def test_hash_computed_then_cached(tracking_db, tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"hello world")
    calls = []

    def compute(p):
        calls.append(str(p))
        return "deadbeef"

    h1 = tracking_db.get_or_compute_hash(f, compute_fn=compute)
    h2 = tracking_db.get_or_compute_hash(f, compute_fn=compute)
    assert h1 == h2 == "deadbeef"
    assert len(calls) == 1  # 2e appel servi par le cache (size, mtime inchangés)


def test_hash_cache_invalidated_on_mtime(tracking_db, tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"hello")
    n = {"c": 0}

    def compute(_p):
        n["c"] += 1
        return f"hash{n['c']}"

    h1 = tracking_db.get_or_compute_hash(f, compute_fn=compute)
    st = f.stat()
    os.utime(f, (st.st_atime, st.st_mtime + 100))  # mtime modifié
    h2 = tracking_db.get_or_compute_hash(f, compute_fn=compute)
    assert h1 != h2
    assert n["c"] == 2  # recalcul forcé par le changement de mtime


def test_default_compute_is_sha256(tracking_db, tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"contenu binaire arbitraire")
    h = tracking_db.get_or_compute_hash(f)
    assert h == hashlib.sha256(b"contenu binaire arbitraire").hexdigest()


def test_read_path_reads_mirror_but_keys_canonical(tracking_db, tmp_path):
    """Régression SSD : le contenu vient de read_path, l'identité reste canonique."""
    canon = tmp_path / "canon.bin"
    mirror = tmp_path / "mirror.bin"
    canon.write_bytes(b"AAAA")
    mirror.write_bytes(b"BBBB")  # même taille, contenu DIFFÉRENT

    h = tracking_db.get_or_compute_hash(canon, read_path=mirror)
    assert h == hashlib.sha256(b"BBBB").hexdigest()  # lu depuis le miroir

    rows = tracking_db._conn.execute(
        "SELECT path FROM files WHERE hash = ?", (h,)).fetchall()
    paths = [r["path"] for r in rows]
    from connaissance.core.tracking import canon_file_path
    assert canon_file_path(canon) in paths   # indexé sous le canonique
    assert str(mirror) not in paths   # jamais sous le miroir


def test_simhash_default_and_cache(tracking_db, tmp_path):
    f = tmp_path / "t.md"
    f.write_text("contenu de transcription répété pour produire un simhash stable " * 3,
                 encoding="utf-8")
    rel = "Transcriptions/Documents/t.md"

    h1 = tracking_db.get_or_compute_simhash(f, rel)
    assert h1 is not None and len(h1) == 16
    from_hex(h1)  # hex valide

    row = tracking_db._conn.execute(
        "SELECT simhash FROM text_simhash WHERE rel_path = ?", (rel,)).fetchone()
    assert row is not None and row["simhash"] == h1  # indexé sur le chemin logique

    h2 = tracking_db.get_or_compute_simhash(f, rel)
    assert h2 == h1


def test_simhash_empty_file_returns_none(tracking_db, tmp_path):
    f = tmp_path / "empty.md"
    f.write_text("", encoding="utf-8")
    assert tracking_db.get_or_compute_simhash(f, "Transcriptions/Documents/empty.md") is None


def test_simhash_nfc_normalized_key(tracking_db, tmp_path):
    # Écriture clé NFD (walk macOS) puis relecture NFC → cache hit, une seule ligne.
    import unicodedata
    f = tmp_path / "doc.md"
    f.write_text("transcription accentuée répétée pour un simhash stable " * 3,
                 encoding="utf-8")
    rel_nfd = unicodedata.normalize("NFD", "Transcriptions/Documents/relevé.md")
    rel_nfc = unicodedata.normalize("NFC", "Transcriptions/Documents/relevé.md")
    assert rel_nfd != rel_nfc
    h1 = tracking_db.get_or_compute_simhash(f, rel_nfd)
    h2 = tracking_db.get_or_compute_simhash(f, rel_nfc)
    assert h1 is not None and h1 == h2                       # cache hit malgré NFD↔NFC
    n = tracking_db._conn.execute(
        "SELECT COUNT(*) FROM text_simhash").fetchone()[0]
    assert n == 1                                            # pas de doublon NFD/NFC


def test_doc_simhash_separate_table_from_text_simhash(tracking_db, tmp_path):
    # get_or_compute_doc_simhash écrit dans doc_simhash, pas text_simhash.
    f = tmp_path / "brut.md"
    f.write_text("contenu d'un fichier brut documents répété pour simhash " * 3,
                 encoding="utf-8")
    h = tracking_db.get_or_compute_doc_simhash(f, "organismes/x/2024 relevé.pdf")
    assert h is not None and len(h) == 16
    assert tracking_db._conn.execute(
        "SELECT COUNT(*) FROM doc_simhash").fetchone()[0] == 1
    assert tracking_db._conn.execute(
        "SELECT COUNT(*) FROM text_simhash").fetchone()[0] == 0  # univers séparés
