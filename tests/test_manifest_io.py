"""Tests du loader de manifestes commun (core/manifest_io.py) — pur, sans env."""
import json

import pytest

from connaissance.core import manifest_io as M


def _w(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


def test_load_entries_liste_nue(tmp_path):
    p = _w(tmp_path, "l.json", [{"a": 1}, {"a": 2}])
    env, ent = M.load_entries(p)
    assert env is None
    assert ent == [{"a": 1}, {"a": 2}]


@pytest.mark.parametrize("key", ["entries", "entrees", "items"])
def test_load_entries_enveloppe_toutes_cles(tmp_path, key):
    p = _w(tmp_path, "e.json", {"total": 1, key: [{"x": 1}]})
    env, ent = M.load_entries(p)
    assert isinstance(env, dict)
    assert ent == [{"x": 1}]


def test_load_entries_priorite_entries_sur_entrees(tmp_path):
    # `entries` (classify) prime sur `entrees` (organize) si les deux présents.
    p = _w(tmp_path, "both.json", {"entries": [{"new": 1}], "entrees": [{"old": 1}]})
    _, ent = M.load_entries(p)
    assert ent == [{"new": 1}]


def test_load_entries_restreint_a_une_cle(tmp_path):
    # manifest patch restreint à 'entrees' : un dict {entries} doit donner [].
    p = _w(tmp_path, "x.json", {"entries": [{"b": 2}]})
    env, ent = M.load_entries(p, list_keys=("entrees",))
    assert isinstance(env, dict)
    assert ent == []


def test_load_entries_dict_sans_cle_liste(tmp_path):
    p = _w(tmp_path, "v.json", {"foo": 1})
    env, ent = M.load_entries(p)
    assert isinstance(env, dict)
    assert ent == []


def test_load_entries_liste_non_liste_sous_cle(tmp_path):
    # Une clé reconnue mais dont la valeur n'est pas une liste → [].
    p = _w(tmp_path, "bad.json", {"entries": {"pas": "une liste"}})
    _, ent = M.load_entries(p)
    assert ent == []


def test_unwrap_deballe_premiere_cle():
    assert M.unwrap({"results": [1, 2]}, "results") == [1, 2]
    assert M.unwrap({"requests": [3]}, "results", "requests") == [3]


def test_unwrap_liste_nue_inchangee():
    assert M.unwrap([1, 2], "results") == [1, 2]


def test_unwrap_aucune_cle_presente_rend_data():
    d = {"autre": 9}
    assert M.unwrap(d, "results") is d


def test_unique_dest_inexistant_inchange(tmp_path):
    p = tmp_path / "y.pdf"
    assert M.unique_dest(p) == p


def test_unique_dest_collision_incremente(tmp_path):
    (tmp_path / "x.pdf").write_text("a")
    cand = M.unique_dest(tmp_path / "x.pdf")
    assert cand.name == "x (2).pdf"
    # Et en cascade.
    (tmp_path / "x (2).pdf").write_text("b")
    assert M.unique_dest(tmp_path / "x.pdf").name == "x (3).pdf"
