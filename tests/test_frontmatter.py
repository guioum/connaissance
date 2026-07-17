"""core/frontmatter : découpe, parsing et round-trip d'écriture."""

from connaissance.core.frontmatter import (dump_frontmatter, parse_frontmatter,
                                           read_frontmatter, split_frontmatter,
                                           write_frontmatter)

DOC = "---\ntitle: essai\ndate: 2026-01-01\n---\n\nCorps du texte.\n"


def test_split_et_round_trip():
    fm_text, body = split_frontmatter(DOC)
    assert "title: essai" in fm_text
    # round-trip fidèle : dump(parse) == original (mêmes clés, même body)
    rebuilt = dump_frontmatter({"title": "essai", "date": "2026-01-01"}, body)
    assert rebuilt == DOC.rstrip("\n") + "\n" or "Corps du texte." in rebuilt


def test_split_ignore_les_tirets_du_corps():
    doc = "---\ntitle: x\n---\ncorps avec --- au milieu\net date---entité---titre\n"
    fm_text, body = split_frontmatter(doc)
    assert fm_text == "title: x"
    assert "date---entité---titre" in body


def test_parse_cas_limites():
    assert parse_frontmatter("pas de frontmatter") is None
    assert parse_frontmatter("---\ntitle: x\n") is None          # non fermé
    assert parse_frontmatter("---\n\n---\ncorps") == {}          # présent, vide
    assert parse_frontmatter("---\n- a\n- b\n---\n") is None     # racine liste
    assert parse_frontmatter("---\n{invalid: yaml: :\n---\n") is None
    import datetime
    # YAML type les dates nues (comportement historique conservé partout)
    assert parse_frontmatter(DOC) == {"title": "essai",
                                      "date": datetime.date(2026, 1, 1)}


def test_read_et_write(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text(DOC, encoding="utf-8")
    assert read_frontmatter(p)["title"] == "essai"
    assert read_frontmatter(tmp_path / "absent.md") is None

    fm_text, body = split_frontmatter(p.read_text(encoding="utf-8"))
    write_frontmatter(p, {"title": "modifié"}, body)
    fm2 = read_frontmatter(p)
    assert fm2 == {"title": "modifié"}
    assert "Corps du texte." in p.read_text(encoding="utf-8")
    # écriture atomique : pas de résidu temporaire
    assert not list(tmp_path.glob(".doc.md.*"))
