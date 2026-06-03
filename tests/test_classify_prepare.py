"""Tests de la brique 2 Phase C (commands/classify.prepare) — offline."""
import json
from pathlib import Path

from connaissance.commands import classify as CMD


def _signals_file(tmp_path):
    sigs = {"documents": [{
        "rel": "Classer/2025-09-29 - Banque Nationale - Confirmation paiement.pdf",
        "type": "pdf", "origin_folder": "Municipalité", "type_hint": "paiement",
        "name_keywords": ["banque", "paiement"],
        "dates": {"from_name": "2025-09-29", "metadata": None,
                  "filesystem_created": None, "filesystem_modified": None},
        "title_meta": None,
        "summary": {"keywords": ["taxes", "paiement"],
                    "sentences": ["Confirmation de paiement des taxes."],
                    "entities": {"amounts": ["1 200 $"], "dates": ["2025-09-29"],
                                 "refs": []}, "chars": 80},
        "born_digital": True, "text_source": "pdf_embedded", "pdf_available": True,
    }]}
    p = tmp_path / "sigs.json"
    p.write_text(json.dumps(sigs), encoding="utf-8")
    return p


def test_prepare_builds_batch_requests(tmp_path, monkeypatch):
    docroot = tmp_path / "Documents"
    (docroot / "organismes" / "banque-nationale").mkdir(parents=True)
    (docroot / "personnes" / "melanie-bazin").mkdir(parents=True)
    monkeypatch.setattr(CMD, "DOCUMENTS_DIR", docroot)

    res = CMD.prepare(from_signals=str(_signals_file(tmp_path)))
    assert res["total"] == 1
    assert res["known_entities_count"] == 2

    reqs = json.loads(Path(res["transit_file"]).read_text(encoding="utf-8"))["requests"]
    req = reqs[0]
    assert req["custom_id"].startswith("cls_")
    assert req["model"] == CMD.DEFAULT_MODEL
    assert req["max_tokens"] == CMD.DEFAULT_MAX_TOKENS
    # Le user porte le hint ; la liste d'entités connues est dans le SYSTEM
    # (cacheable). Le system impose aussi la sortie JSON + taxonomie canonique.
    assert "Proposition heuristique" in req["user"]
    assert "Banque Nationale" in req["system"]
    assert "JSON" in req["system"] and "impots" in req["system"]


def test_prepare_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(CMD, "DOCUMENTS_DIR", tmp_path / "Documents")
    sf = _signals_file(tmp_path)
    res = CMD.prepare(from_signals=str(sf), limit=0)
    assert res["total"] == 0


def test_known_entities_deslug(tmp_path, monkeypatch):
    docroot = tmp_path / "Documents"
    (docroot / "organismes" / "air-transat").mkdir(parents=True)
    (docroot / "organismes" / "banque-nationale").mkdir(parents=True)
    monkeypatch.setattr(CMD, "DOCUMENTS_DIR", docroot)
    names = CMD.known_entities()
    assert "Air Transat" in names and "Banque Nationale" in names
