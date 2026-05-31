"""Tests du moteur de quasi-doublons (core/dedup.py) — pur, sans environnement."""
from connaissance.core import dedup


def _sh(text: str) -> int:
    """SimHash en garantissant un int (non vide) — narrow le type pour mypy/pyright."""
    v = dedup.simhash_text(text)
    assert v is not None
    return v


def test_simhash_empty_returns_none():
    assert dedup.simhash_text("") is None
    assert dedup.simhash_text("   \n  ") is None


def test_simhash_deterministic():
    txt = "le renard brun saute par-dessus le chien paresseux"
    assert _sh(txt) == _sh(txt)


def test_simhash_strips_frontmatter():
    # Le frontmatter (métadonnées) ne doit pas influencer le hash du corps.
    body = "contenu identique du document partagé entre les deux"
    a = "---\nsource: /a/x.pdf\n---\n" + body
    b = "---\nsource: /b/y.pdf\n---\n" + body
    assert _sh(a) == _sh(b)


def test_identical_text_distance_zero():
    txt = "facture numéro 4321 montant 250 euros payée le 3 mars"
    assert dedup.hamming(_sh(txt), _sh(txt)) == 0


def test_different_text_distance_positive():
    a = _sh("rapport trimestriel sur les ventes en europe du nord")
    b = _sh("recette de tarte aux pommes avec pâte feuilletée maison")
    assert dedup.hamming(a, b) > dedup.DEFAULT_THRESHOLD


def test_near_text_closer_than_unrelated():
    base = _sh("relevé bancaire janvier solde 1000 dépôt 200 retrait 50 " * 3)
    near = _sh("relevé bancaire janvier solde 1000 dépôt 200 retrait 51 " * 3)
    far = _sh("invitation anniversaire samedi 18h chez paul apportez gâteau " * 3)
    assert dedup.hamming(base, near) < dedup.hamming(base, far)


def test_cluster_groups_and_excludes_singletons():
    a = _sh("document alpha identique répété pour le test du clustering")
    b = _sh("document alpha identique répété pour le test du clustering")
    c = _sh("tout autre sujet sans rapport recette cuisine vacances soleil")
    clusters = dedup.cluster_by_hamming([a, b, c], threshold=0)
    assert clusters == [[0, 1]]  # a,b ensemble ; c (singleton) exclu


def test_cluster_empty_when_all_distinct():
    vals = [
        _sh("premier sujet complètement différent des autres alpha"),
        _sh("deuxième sujet complètement différent beta gamma delta"),
        _sh("troisième chose totalement distincte omega sigma tau"),
    ]
    assert dedup.cluster_by_hamming(vals, threshold=0) == []


def test_hex_roundtrip():
    v = _sh("un texte quelconque pour valider la sérialisation hex")
    h = dedup.to_hex(v)
    assert len(h) == 16
    assert dedup.from_hex(h) == v
