"""OCR local (commands/ocr + core/ocr_local). Le moteur Vision est macOS-only ;
on teste ce qui est pur (parsing frontmatter, disponibilité)."""
from connaissance.commands import ocr as O
from connaissance.core import ocr_local


def test_read_frontmatter():
    c = "---\nsource: Documents/x.pdf\nocr_engine: vision-local\nocr_confidence: 0.85\n---\nTexte OCR."
    fm = O._read_frontmatter(c)
    assert fm["ocr_engine"] == "vision-local"
    assert float(fm["ocr_confidence"]) == 0.85
    assert O._read_frontmatter("pas de frontmatter") == {}


def test_available_returns_bool():
    assert isinstance(ocr_local.available(), bool)


def test_ocr_images_classifies_by_text_density(tmp_path, monkeypatch, tracking_db):
    """La densité de texte décide document(reçu) vs photo — sans exclusion EXIF."""
    from connaissance.commands import ocr as O
    docs = tmp_path / "Documents"
    trans = tmp_path / "Transcriptions" / "Documents"
    docs.mkdir(parents=True)
    (docs / "recu.png").write_bytes(b"img")        # « reçu » (beaucoup de texte)
    (docs / "souvenir.jpg").write_bytes(b"img")     # photo (pas de texte)
    monkeypatch.setattr(O, "DOCUMENTS_DIR", docs)
    monkeypatch.setattr(O, "TRANSCRIPTIONS_DIR", trans)
    monkeypatch.setattr(O, "documents_read_path", lambda p: p)
    monkeypatch.setattr(O._ocr, "available", lambda: True)

    def fake_ocr(p, max_pages=1):
        if "recu" in str(p):
            return {"text": "FACTURE\nTotal 12,34$\nMerci\nReçu N°7", "confidence": 0.95}
        return {"text": "", "confidence": 0.0}      # photo souvenir
    monkeypatch.setattr(O._ocr, "ocr_file", fake_ocr)
    monkeypatch.setattr(O, "register_document", lambda *a, **k: None)

    res = O.ocr_images(min_chars=10, min_lines=3, db=tracking_db)
    assert res["documents_images"] == 1
    assert res["non_documents"] == 1
    t = trans / "recu.md"
    assert t.exists() and "ocr_kind: image" in t.read_text(encoding="utf-8")
    assert not (trans / "souvenir.md").exists()
