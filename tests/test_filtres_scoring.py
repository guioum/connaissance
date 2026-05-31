"""Tests du scoring courriels (core/filtres.py).

On injecte des configs de scoring minimales et ciblées (``_scoring_config``)
plutôt que de dépendre du template packagé — chaque test isole UN signal et
vérifie son effet exact. ``corps_min: 0`` désactive le signal « corps
quasi-vide » (actif par défaut) pour ne pas polluer les assertions.
"""
from connaissance.core import filtres


def make_filtres(scoring):
    scoring = dict(scoring)
    scoring.setdefault("seuils_numeriques", {"corps_min": 0})
    f = filtres.Filtres(config_path=filtres.TEMPLATE_FILTRES)
    f._scoring_config = scoring
    return f


def test_empty_config_returns_zero():
    f = filtres.Filtres(config_path=filtres.TEMPLATE_FILTRES)
    f._scoring_config = {}
    assert f.score_courriel({"from": "x@y.com", "body": "court"}) == (0, [])


def test_marketing_domain_is_negative():
    f = make_filtres({"poids": {"adresse_marketing": -2},
                      "domaines_marketing": ["promo.test"]})
    score, reasons = f.score_courriel({"from": "news@promo.test"})
    assert score == -2
    assert any("marketing" in r for r in reasons)


def test_personal_domain_is_positive():
    f = make_filtres({"poids": {"domaine_personnel": 2},
                      "domaines_personnels": ["me.test"]})
    score, _ = f.score_courriel({"from": "ami@me.test"})
    assert score == 2


def test_noreply_is_negative():
    # `patterns_noreply: []` désactive le 2e signal noreply ("sans actionnable",
    # actif par défaut) pour isoler le signal noreply hardcodé.
    f = make_filtres({"poids": {"noreply": -1}, "patterns_noreply": []})
    score, _ = f.score_courriel({"from": "noreply@service.test"})
    assert score == -1


def test_document_attachment_is_positive():
    f = make_filtres({"poids": {"piece_jointe_document": 2}})
    score, _ = f.score_courriel({
        "from": "a@b.test",
        "attachments": [{"filename": "facture.pdf"}],
    })
    assert score == 2


def test_attachments_none_does_not_crash():
    """Régression : `attachments: None` (champ vide) ne doit pas planter."""
    f = make_filtres({"poids": {"piece_jointe_document": 2}})
    score, _ = f.score_courriel({"from": "a@b.test", "attachments": None})
    assert score == 0


def test_signals_accumulate():
    f = make_filtres({
        "poids": {"adresse_marketing": -2, "domaine_personnel": 3},
        "domaines_marketing": ["promo.test"],
        "domaines_personnels": ["promo.test"],  # même domaine déclenche les deux
    })
    score, _ = f.score_courriel({"from": "x@promo.test"})
    assert score == 1  # -2 + 3
