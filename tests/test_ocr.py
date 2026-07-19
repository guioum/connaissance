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


def test_ocr_images_resume_skips_logged(tmp_path, monkeypatch, tracking_db):
    """Reprise : une image déjà jugée (journal DB) est sautée au run suivant —
    pas de re-OCR des photos rejetées."""
    docs = tmp_path / "Documents"; trans = tmp_path / "Transcriptions" / "Documents"
    docs.mkdir(parents=True)
    (docs / "photo.jpg").write_bytes(b"img")
    monkeypatch.setattr(O, "DOCUMENTS_DIR", docs)
    monkeypatch.setattr(O, "TRANSCRIPTIONS_DIR", trans)
    monkeypatch.setattr(O, "documents_read_path", lambda p: p)
    monkeypatch.setattr(O._ocr, "available", lambda: True)
    calls = {"n": 0}
    def fake_ocr(p, max_pages=1):
        calls["n"] += 1
        return {"text": "", "confidence": 0.0}        # photo (non-doc)
    monkeypatch.setattr(O._ocr, "ocr_file", fake_ocr)

    r1 = O.ocr_images(min_chars=10, db=tracking_db)
    assert r1["non_documents"] == 1 and calls["n"] == 1
    # 2e passage : l'image est journalisée → sautée, pas de nouvel OCR
    r2 = O.ocr_images(min_chars=10, db=tracking_db)
    assert calls["n"] == 1                              # ocr_file NON rappelé
    assert r2["skipped"]["deja_traite"] == 1


def _put_signals(db, rel, **fields):
    import json
    pkt = {"_v": 99, "rel": rel, **fields}      # _v non vérifié par all_doc_signals
    db._conn.execute(
        "INSERT INTO doc_signals (rel_path, signals, size, mtime) VALUES (?,?,?,?)",
        (rel, json.dumps(pkt), 0, 0))
    db._conn.commit()


def test_transcribe_plan_worklist(tmp_path, monkeypatch, tracking_db):
    """Worklist Mistral : upgrade vision-local + scanné manquant ≤ N pages ;
    born-digital exclu ; >N pages déféré ; mistral déjà fait ignoré."""
    docs = tmp_path / "Documents"
    trans = tmp_path / "Transcriptions" / "Documents"
    docs.mkdir(parents=True)
    trans.mkdir(parents=True)
    monkeypatch.setattr(O, "DOCUMENTS_DIR", docs)
    monkeypatch.setattr(O, "TRANSCRIPTIONS_DIR", trans)

    def _trans(rel, engine):
        p = trans / (rel[:-4] + ".md")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\nocr_engine: {engine}\n---\nTexte.", encoding="utf-8")

    # vision-local, 3 pages → upgrade
    _put_signals(tracking_db, "upg.pdf", type="pdf", text_source="ocr_cache",
                 born_digital=False, pages=3)
    _trans("upg.pdf", "vision-local")
    # scanné sans transcription, 5 pages → missing
    _put_signals(tracking_db, "miss.pdf", type="pdf", text_source="none",
                 born_digital=False, pages=5)
    # born-digital → exclu
    _put_signals(tracking_db, "born.pdf", type="pdf", text_source="pdf_embedded",
                 born_digital=True, pages=2)
    # vision-local 40 pages → déféré (> max_pages)
    _put_signals(tracking_db, "big.pdf", type="pdf", text_source="ocr_cache",
                 born_digital=False, pages=40)
    _trans("big.pdf", "vision-local")
    # déjà Mistral → ignoré
    _put_signals(tracking_db, "done.pdf", type="pdf", text_source="ocr_cache",
                 born_digital=False, pages=1)
    _trans("done.pdf", "mistral")

    res = O.transcribe_plan(max_pages=10, db=tracking_db)
    rels = {e["rel"]: e for e in res["worklist"]}
    assert set(rels) == {"upg.pdf", "miss.pdf"}
    assert rels["upg.pdf"]["reason"] == "upgrade_vision"
    assert rels["miss.pdf"]["reason"] == "missing"
    assert res["counts"]["born_digital_skip"] == 1
    assert res["counts"]["already_mistral"] == 1
    assert res["deferred_count"] == 1
    assert res["estimated_pages"] == 8        # 3 + 5
    # Référence la constante (pas le tarif en dur) : le prix par page suit le
    # modèle Mistral épinglé (OCR 4 depuis 2026-07-19).
    assert res["estimated_cost_usd"] == round(8 * O._MISTRAL_PAGE_COST, 2)


def test_transcribe_plan_manifest_fields_and_dedup(tmp_path, monkeypatch, tracking_db):
    """Entrées au format scan (source/read_source/transcription) + dédup des
    lignes-fantômes (variantes NFC/casse du même fichier)."""
    docs = tmp_path / "Documents"
    trans = tmp_path / "Transcriptions" / "Documents"
    docs.mkdir(parents=True); trans.mkdir(parents=True)
    monkeypatch.setattr(O, "DOCUMENTS_DIR", docs)
    monkeypatch.setattr(O, "TRANSCRIPTIONS_DIR", trans)
    # read_source = miroir : on simule en renvoyant un chemin /ssd/<rel>
    monkeypatch.setattr(O, "documents_read_path", lambda p: f"/ssd/{p.name}")

    # deux lignes pour le MÊME fichier (casse différente) → 1 seule entrée
    _put_signals(tracking_db, "Releve.pdf", type="pdf", text_source="none",
                 born_digital=False, pages=2)
    _put_signals(tracking_db, "releve.pdf", type="pdf", text_source="none",
                 born_digital=False, pages=2)

    res = O.transcribe_plan(max_pages=10, db=tracking_db)
    assert res["worklist_count"] == 1
    assert res["counts"]["phantom_dupes"] == 1
    e = res["to_transcribe"][0]
    assert e["source"] == str(docs / e["rel"])
    assert e["read_source"].startswith("/ssd/")
    assert e["transcription"].endswith(".md")


def test_transcribe_plan_output_file(tmp_path, monkeypatch, tracking_db):
    """--output-file écrit un manifeste to_transcribe et renvoie un résumé compact."""
    docs = tmp_path / "Documents"; trans = tmp_path / "Transcriptions" / "Documents"
    docs.mkdir(parents=True); trans.mkdir(parents=True)
    monkeypatch.setattr(O, "DOCUMENTS_DIR", docs)
    monkeypatch.setattr(O, "TRANSCRIPTIONS_DIR", trans)
    monkeypatch.setattr(O, "documents_read_path", lambda p: str(p))
    _put_signals(tracking_db, "a.pdf", type="pdf", text_source="none",
                 born_digital=False, pages=1)
    out = tmp_path / "manifest.json"
    res = O.transcribe_plan(max_pages=10, output_file=str(out), db=tracking_db)
    import json
    man = json.loads(out.read_text())
    assert man["to_transcribe"][0]["rel"] == "a.pdf"
    # le retour est compact (résumé), pas la liste complète inline
    assert "sample" in res and res["worklist_count"] == 1


def test_transcribe_plan_excludes_encrypted_broken(tmp_path, monkeypatch, tracking_db):
    """Les PDF protégés/corrompus (pdf_status) sont écartés avant l'OCR."""
    docs = tmp_path / "Documents"; trans = tmp_path / "Transcriptions" / "Documents"
    docs.mkdir(parents=True); trans.mkdir(parents=True)
    monkeypatch.setattr(O, "DOCUMENTS_DIR", docs)
    monkeypatch.setattr(O, "TRANSCRIPTIONS_DIR", trans)
    monkeypatch.setattr(O, "documents_read_path", lambda p: str(p))
    _put_signals(tracking_db, "ok.pdf", type="pdf", text_source="none",
                 born_digital=False, pages=2, pdf_status="ok")
    _put_signals(tracking_db, "locked.pdf", type="pdf", text_source="none",
                 born_digital=False, pages=1, pdf_status="encrypted")
    _put_signals(tracking_db, "broken.pdf", type="pdf", text_source="none",
                 born_digital=False, pages=1, pdf_status="unreadable")
    res = O.transcribe_plan(max_pages=10, db=tracking_db)
    rels = {e["rel"] for e in res["to_transcribe"]}
    assert rels == {"ok.pdf"}
    assert res["counts"]["encrypted_or_broken"] == 2
    assert {e["rel"] for e in res["excluded"]} == {"locked.pdf", "broken.pdf"}


def test_transcribe_plan_excludes_non_ocr_formats(tmp_path, monkeypatch, tracking_db):
    """Un ebook/markdown avec transcription n'est PAS une cible OCR (Mistral n'a
    rien à OCRiser) ; seuls PDF + images le sont."""
    docs = tmp_path / "Documents"; trans = tmp_path / "Transcriptions" / "Documents"
    docs.mkdir(parents=True); trans.mkdir(parents=True)
    monkeypatch.setattr(O, "DOCUMENTS_DIR", docs)
    monkeypatch.setattr(O, "TRANSCRIPTIONS_DIR", trans)
    monkeypatch.setattr(O, "documents_read_path", lambda p: str(p))
    # ont tous une transcription (ocr_cache), mais formats différents
    _put_signals(tracking_db, "scan.pdf", type="pdf", text_source="ocr_cache",
                 born_digital=False, pages=1, pdf_status="ok")
    _put_signals(tracking_db, "book.epub", type="epub", text_source="ocr_cache")
    _put_signals(tracking_db, "note.markdown", type="markdown", text_source="ocr_cache")
    _put_signals(tracking_db, "recu.png", type="png", text_source="ocr_cache")
    res = O.transcribe_plan(max_pages=10, db=tracking_db)
    rels = {e["rel"] for e in res["to_transcribe"]}
    assert rels == {"scan.pdf", "recu.png"}          # epub + markdown exclus
    assert res["counts"]["non_ocr_type_skip"] == 2


def test_classify_pdf_error():
    from connaissance.core.signals import _classify_pdf_error
    assert _classify_pdf_error(Exception("Incorrect password error")) == "encrypted"
    assert _classify_pdf_error(Exception("Failed to load (encrypted)")) == "encrypted"
    assert _classify_pdf_error(Exception("Format error: not a PDF")) == "unreadable"


def test_transcribe_plan_upgrade_only(tmp_path, monkeypatch, tracking_db):
    """--upgrade-only exclut les scannés sans transcription."""
    docs = tmp_path / "Documents"
    trans = tmp_path / "Transcriptions" / "Documents"
    docs.mkdir(parents=True)
    trans.mkdir(parents=True)
    monkeypatch.setattr(O, "DOCUMENTS_DIR", docs)
    monkeypatch.setattr(O, "TRANSCRIPTIONS_DIR", trans)
    _put_signals(tracking_db, "miss.pdf", type="pdf", text_source="none",
                 born_digital=False, pages=2)
    res = O.transcribe_plan(max_pages=10, include_missing=False, db=tracking_db)
    assert res["worklist_count"] == 0
