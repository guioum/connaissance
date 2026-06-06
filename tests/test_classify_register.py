"""Tests de la brique 4 Phase C (commands/classify.register) — offline."""
import json

from connaissance.commands import classify as CMD


def _files(tmp_path, prep, results):
    pf = tmp_path / "prep.json"
    pf.write_text(json.dumps(prep), encoding="utf-8")
    rf = tmp_path / "res.json"
    rf.write_text(json.dumps(results), encoding="utf-8")
    return str(rf), str(pf)


def test_register_auto_and_attente(tmp_path, monkeypatch):
    monkeypatch.setattr(CMD, "chercher_alias", lambda *a, **k: None)
    prep = {"requests": [
        {"custom_id": "cls_a", "_rel": "Classer/x/doc1.pdf", "_hint": {"entity": "Bnc"}},
        {"custom_id": "cls_b", "_rel": "Classer/y/scan2.pdf", "_hint": {"entity": "divers"}},
    ]}
    results = {"results": [
        {"custom_id": "cls_a", "content":
         '```json\n{"entity":"Banque Nationale","entity_type":"organismes",'
         '"category":"banque","date":"2024-03-15","title":"Relevé de compte",'
         '"sujet":"banque","confidence":"high","reason":"x"}\n```'},
        {"custom_id": "cls_b", "content":
         '{"entity":"truc","entity_type":"divers","category":"divers",'
         '"date":null,"title":"?","confidence":"low","reason":"y"}'},
    ]}
    rf, pf = _files(tmp_path, prep, results)
    res = CMD.register(rf, pf)

    assert res["total"] == 2 and res["auto"] == 1 and res["attente"] == 1
    a = next(e for e in res["entries"] if e["custom_id"] == "cls_a")
    assert a["status"] == "auto"
    assert a["dest"] == "organismes/banque-nationale/2024-03-15 releve-de-compte.pdf"
    b = next(e for e in res["entries"] if e["custom_id"] == "cls_b")
    assert b["status"] == "attente" and b["dest"] is None
    assert "confiance_basse" in b["reasons"]


def test_register_invalid_category_to_attente(tmp_path, monkeypatch):
    monkeypatch.setattr(CMD, "chercher_alias", lambda *a, **k: None)
    prep = {"requests": [{"custom_id": "c", "_rel": "x/d.pdf", "_hint": {}}]}
    results = {"results": [{"custom_id": "c", "content":
        '{"entity":"X","entity_type":"organismes","category":"BANANE",'
        '"date":"2024-01-01","title":"t","confidence":"high"}'}]}
    rf, pf = _files(tmp_path, prep, results)
    e = CMD.register(rf, pf)["entries"][0]
    assert e["category"] is None and e["status"] == "attente"
    assert "catégorie_invalide" in e["reasons"]


def test_register_parse_failure_to_attente(tmp_path, monkeypatch):
    monkeypatch.setattr(CMD, "chercher_alias", lambda *a, **k: None)
    prep = {"requests": [{"custom_id": "c", "_rel": "x/d.pdf",
                          "_hint": {"entity": "Fallback Co"}}]}
    results = {"results": [{"custom_id": "c", "content": "pas du json du tout"}]}
    rf, pf = _files(tmp_path, prep, results)
    e = CMD.register(rf, pf)["entries"][0]
    assert e["status"] == "attente"
    assert "parse_échoué" in e["reasons"]
    assert e["entity"] == "Fallback Co"   # repli sur le hint


def test_register_divers_with_sujet_is_attente_not_auto(tmp_path, monkeypatch):
    # entité divers → attente même en confiance haute (pas d'entité fiable).
    monkeypatch.setattr(CMD, "chercher_alias", lambda *a, **k: None)
    prep = {"requests": [{"custom_id": "c", "_rel": "x/d.pdf", "_hint": {}}]}
    results = {"results": [{"custom_id": "c", "content":
        '{"entity":"Devis","entity_type":"divers","category":"logement",'
        '"date":"2025-01-01","title":"Devis travaux","sujet":"maison",'
        '"confidence":"high"}'}]}
    rf, pf = _files(tmp_path, prep, results)
    e = CMD.register(rf, pf)["entries"][0]
    assert e["status"] == "attente" and "entité_divers" in e["reasons"]


def test_register_content_liste_de_blocs_messages(tmp_path, monkeypatch):
    # L'API Messages renvoie content=[{type:text, text:...}] : doit être parsé
    # comme une chaîne (tolérance #8), pas basculer en parse_échoué.
    monkeypatch.setattr(CMD, "chercher_alias", lambda *a, **k: None)
    prep = {"requests": [{"custom_id": "c", "_rel": "x/d.pdf", "_hint": {}}]}
    results = {"results": [{"custom_id": "c", "content": [
        {"type": "text", "text":
            '{"entity":"Hydro","entity_type":"organismes","category":"banque",'
            '"date":"2024-06-01","title":"Facture","confidence":"high"}'},
    ]}]}
    rf, pf = _files(tmp_path, prep, results)
    e = CMD.register(rf, pf)["entries"][0]
    assert e["status"] == "auto"
    assert e["entity_type"] == "organismes"
    assert "parse_échoué" not in e["reasons"]


def test_register_type_inconnu_reste_inconnus_pas_divers(tmp_path, monkeypatch):
    # Un entity_type non canonique retombe sur 'inconnus' (#5), pas 'divers'.
    monkeypatch.setattr(CMD, "chercher_alias", lambda *a, **k: None)
    prep = {"requests": [{"custom_id": "c", "_rel": "x/d.pdf", "_hint": {}}]}
    results = {"results": [{"custom_id": "c", "content":
        '{"entity":"Truc","entity_type":"BIDON","category":"divers",'
        '"date":"2024-01-01","title":"t","confidence":"high"}'}]}
    rf, pf = _files(tmp_path, prep, results)
    e = CMD.register(rf, pf)["entries"][0]
    assert e["entity_type"] == "inconnus"
    assert e["status"] == "attente"
    assert "entité_inconnus" in e["reasons"]
