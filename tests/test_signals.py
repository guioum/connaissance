"""Tests du moteur de signaux Phase B (core/signals.py).

Les chemins stdlib (texte, docx, cache OCR, dataless) sont couverts sans
environnement. Le chemin PDF (pypdfium2) n'est testé que si la lib est présente.
"""
import zipfile

from connaissance.core import signals as SIG


def _make_docx(path, *, title="", author="", body="Texte du document."):
    core = (
        '<?xml version="1.0"?>'
        '<cp:coreProperties '
        'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/">'
        f"<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>"
        "<dcterms:created>2024-02-03T10:00:00Z</dcterms:created>"
        "</cp:coreProperties>"
    )
    doc = (
        '<?xml version="1.0"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{body}</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("docProps/core.xml", core)
        z.writestr("word/document.xml", doc)


def test_plain_text_signals(tmp_path):
    p = tmp_path / "2024-03-15 facture loyer.txt"
    p.write_text("Facture de loyer. Montant 1 200,00 $ payable le 2024-04-01.",
                 encoding="utf-8")
    sig = SIG.extract_signals(p, rel="organismes/proprio/2024-03-15 facture loyer.txt",
                              read_path=p)
    assert sig["text_source"] == "plain"
    assert sig["type"] == "txt"
    assert sig["type_hint"] == "facture"
    assert sig["dates"]["from_name"] == "2024-03-15"
    assert sig["origin_folder"] == "proprio"
    assert sig["summary"]["chars"] > 0
    assert sig["summary"]["entities"]["amounts"]


def test_docx_text_and_metadata(tmp_path):
    p = tmp_path / "rapport.docx"
    _make_docx(p, title="Rapport annuel", author="Guillaume",
               body="Rapport annuel des activités. Bilan financier positif.")
    sig = SIG.extract_signals(p, read_path=p)
    assert sig["text_source"] == "office"
    assert sig["title_meta"] == "Rapport annuel"
    assert sig["author_meta"] == "Guillaume"
    assert "rapport" in sig["summary"]["keywords"]


def test_ocr_cache_takes_priority(tmp_path):
    p = tmp_path / "scan.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    sig = SIG.extract_signals(p, read_path=p,
                              ocr_cache_text="Contenu déjà transcrit du relevé.")
    assert sig["text_source"] == "ocr_cache"
    assert sig["summary"]["chars"] > 0


def test_dataless_no_read_path_skips_content(tmp_path):
    # read_path=None (dataless sans miroir) → pas de lecture contenu, mais
    # nom/chemin/dates restent disponibles.
    p = tmp_path / "2023-01-01 contrat client.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    sig = SIG.extract_signals(p, read_path=None)
    assert sig["text_source"] == "none"
    assert sig["type_hint"] == "contrat"
    assert sig["dates"]["from_name"] == "2023-01-01"
    assert sig["dates"]["filesystem_modified"] is not None  # stat OK, sans download


def test_pdf_without_pypdfium_degrades(tmp_path, monkeypatch):
    # Simuler pypdfium2 absent : born_digital indécidable, pas de texte.
    monkeypatch.setattr(SIG, "_pdfium", None)
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    sig = SIG.extract_signals(p, read_path=p)
    assert sig["text_source"] == "none"
    assert sig["born_digital"] is None
    assert sig["pdf_available"] is False


# --- cache tracking.db ------------------------------------------------------

def test_get_or_compute_signals_caches(tracking_db, tmp_path):
    p = tmp_path / "doc.txt"
    p.write_text("contenu", encoding="utf-8")
    calls = []

    from connaissance.core.signals import SIGNALS_SCHEMA_VERSION

    def compute(_p):
        calls.append(1)
        # Le paquet DOIT porter `_v` à la version courante, sinon le cache le
        # considère périmé et recalcule (invalidation par version du schéma).
        return {"_v": SIGNALS_SCHEMA_VERSION, "rel": "doc.txt", "ok": True}

    a = tracking_db.get_or_compute_signals(p, "doc.txt", compute)
    b = tracking_db.get_or_compute_signals(p, "doc.txt", compute)
    assert a == b == {"_v": SIGNALS_SCHEMA_VERSION, "rel": "doc.txt", "ok": True}
    assert len(calls) == 1   # 2e appel servi par le cache

    # mtime change → recalcul
    import os
    os.utime(p, (0, 0))
    tracking_db.get_or_compute_signals(p, "doc.txt", compute)
    assert len(calls) == 2


# --- commande documents signals --------------------------------------------

def test_signals_command_walks_group_a(tmp_path, monkeypatch, tracking_db):
    from connaissance.commands import signals as CMD
    root = tmp_path / "Documents"
    (root / "Classer" / "Impôts 2024").mkdir(parents=True)
    (root / "Classer" / "Impôts 2024" / "2024-03-15 facture loyer.txt").write_text(
        "Facture de loyer. Montant 1 200,00 $ payable.", encoding="utf-8")
    (root / "Classer" / "photo.jpg").write_bytes(b"x")        # média → ignoré
    (root / "Classer" / "id_rsa").write_text("KEY", encoding="utf-8")  # secret nom

    monkeypatch.setattr(CMD, "DOCUMENTS_DIR", root)
    monkeypatch.setattr(CMD._filtres, "load_quarantine_set", lambda: set())
    res = CMD.scan(db=tracking_db)

    assert res["total"] == 1
    doc = res["documents"][0]
    assert doc["rel"].endswith("2024-03-15 facture loyer.txt")
    assert doc["text_source"] == "plain"
    assert doc["type_hint"] == "facture"
    assert doc["origin_folder"] == "Impôts 2024"
    assert doc["summary"]["entities"]["amounts"]


def _make_xlsx(path, strings):
    import zipfile
    sst = '<?xml version="1.0"?><sst>' + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/sharedStrings.xml", sst)
        z.writestr("[Content_Types].xml", "<Types/>")


def _make_pptx(path, runs):
    import zipfile
    slide = '<?xml version="1.0"?><p:sld><p:cSld>' + "".join(f"<a:t>{r}</a:t>" for r in runs) + "</p:cSld></p:sld>"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("ppt/slides/slide1.xml", slide)


def test_xlsx_text_stdlib(tmp_path):
    from connaissance.core.signals import _xlsx_text
    p = tmp_path / "b.xlsx"
    _make_xlsx(p, ["Facture", "Banque Nationale", "Total 1 234,56 $"])
    t = _xlsx_text(p)
    assert "Facture" in t and "Banque Nationale" in t and "1 234,56" in t


def test_pptx_text_stdlib(tmp_path):
    from connaissance.core.signals import _pptx_text
    p = tmp_path / "d.pptx"
    _make_pptx(p, ["Stratégie données", "Jemena", "Plan 2024"])
    t = _pptx_text(p)
    assert "Stratégie données" in t and "Jemena" in t


def test_scan_includes_image_documents_only(tmp_path, monkeypatch, tracking_db):
    """scan() : une image AVEC transcription (= document détecté par ocr-images)
    entre dans doc_signals ; une image SANS transcription (photo souvenir) non."""
    from connaissance.commands import signals as CSIG
    docs = tmp_path / "Documents"
    trans = tmp_path / "Transcriptions" / "Documents"
    docs.mkdir(parents=True)
    trans.mkdir(parents=True)
    (docs / "recu.png").write_bytes(b"img-bytes")        # document
    (docs / "souvenir.jpg").write_bytes(b"img-bytes")    # photo
    (docs / "lettre.pdf").write_bytes(b"%PDF-1.4")       # doc classique
    # transcription seulement pour le reçu
    (trans / "recu.md").write_text("---\nocr_engine: vision-local\n---\nFACTURE 12$",
                                   encoding="utf-8")
    monkeypatch.setattr(CSIG, "DOCUMENTS_DIR", docs)
    monkeypatch.setattr(CSIG, "TRANSCRIPTIONS_DIR", trans)
    monkeypatch.setattr(CSIG, "documents_read_path", lambda p: p)
    monkeypatch.setattr(CSIG, "is_dataless", lambda p: False)
    monkeypatch.setattr(CSIG._filtres, "load_quarantine_set", lambda: set())

    res = CSIG.scan(db=tracking_db)
    rels = {p["rel"] for p in res["documents"]}
    assert "recu.png" in rels            # image-document inclus
    assert "souvenir.jpg" not in rels    # photo souvenir exclue
    assert res["skipped"]["image_non_document"] == 1
    # le reçu a bien le texte du cache OCR
    recu = next(p for p in res["documents"] if p["rel"] == "recu.png")
    assert recu["text_source"] == "ocr_cache"
    assert recu["type"] == "png"
