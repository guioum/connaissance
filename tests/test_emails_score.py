"""`emails score` : exposer le scoring calibré à une boîte VIVANTE.

`score_courriel()` est une fonction pure ; ce verbe la prête au skill de
capture, qui lit Fastmail et non les archives mbox. Les tests portent sur ce
que la frontière MCP peut casser — la forme de l'expéditeur — et sur le contrat
de sortie, pas sur les poids eux-mêmes (couverts par test_filtres_scoring).
"""
import json

import pytest

from connaissance.commands import emails
from connaissance.core import filtres

SCORING = {
    "seuils": {"capturer": 0, "ignorer": -1},
    "poids": {"adresse_marketing": -5, "reseau_social": -3,
              "domaine_personnel": 2, "sujet_actionnable": 3},
    "seuils_numeriques": {"corps_min": 0},
    "domaines_marketing": ["members.netflix.com"],
    "domaines_reseaux_sociaux": ["facebookmail.com"],
    "domaines_personnels": ["fmrq.qc.ca"],
    "patterns_sujet_actionnable": ["avis de cotisation"],
}


@pytest.fixture(autouse=True)
def _scoring(monkeypatch):
    def _fabrique(*a, **k):
        f = filtres.Filtres(config_path=filtres.TEMPLATE_FILTRES)
        f._scoring_config = dict(SCORING)
        return f
    monkeypatch.setattr(emails, "Filtres", _fabrique)


# --- La forme de l'expéditeur : le point où le MCP peut tout casser ---

@pytest.mark.parametrize("expediteur", [
    "info@members.netflix.com",                              # adresse nue
    "Netflix <info@members.netflix.com>",                    # en-tête complet
    {"name": "Netflix", "email": "info@members.netflix.com"},  # dict
    [{"name": "Netflix", "email": "info@members.netflix.com"}],  # liste Fastmail
])
def test_le_domaine_est_reconnu_quelle_que_soit_la_forme(expediteur):
    """Un en-tête « Nom <adresse> » laissait un « > » collé au domaine.

    Le signal marketing disparaissait alors en silence et le message devenait
    capturable : un résultat qui n'a l'air de rien et qui est faux.
    """
    out = emails.score_messages([{"id": "x", "from": expediteur, "subject": "Souvenirs"}])
    assert out["results"][0]["decision"] == "ignorer"
    assert any("marketing" in r for r in out["results"][0]["reasons"])


def test_expediteur_absent_ne_leve_pas():
    out = emails.score_messages([{"id": "x", "subject": "Sans expéditeur"}])
    assert out["results"][0]["score"] == 0


# --- Contrat de sortie ---

def test_les_trois_decisions_suivent_les_seuils():
    out = emails.score_messages([
        {"id": "pub", "from": "a@members.netflix.com", "subject": "Promo"},
        {"id": "neutre", "from": "quelquun@exemple.org", "subject": "Bonjour"},
        {"id": "utile", "from": "b@revenuquebec.ca", "subject": "Avis de cotisation TPS"},
    ])
    par_id = {r["id"]: r for r in out["results"]}
    assert par_id["pub"]["decision"] == "ignorer"
    assert par_id["neutre"]["decision"] == "capturer"   # seuil capturer = 0
    assert par_id["utile"]["decision"] == "capturer"
    assert out["repartition"]["ignorer"] == 1
    assert out["seuils"] == {"capturer": 0, "ignorer": -1}


def test_les_raisons_sont_rendues_pour_calibrer():
    """`reasons` est ce qui rend un écart auditable — et corrigeable par atome."""
    out = emails.score_messages([{"id": "x", "from": "a@facebookmail.com", "subject": "s"}])
    assert out["results"][0]["reasons"] == ["réseau social (facebookmail.com) [-3]"]


def test_sans_corps_compte_les_messages_scores_sur_un_apercu():
    """Un aperçu de 200 caractères fait mentir tous les signaux de corps.

    Le compteur dit à l'appelant quand relire les corps avant de trancher.
    """
    out = emails.score_messages([
        {"id": "a", "from": "x@exemple.org", "subject": "s"},
        {"id": "b", "from": "y@exemple.org", "subject": "s", "body": "un corps"},
    ])
    assert out["sans_corps"] == 1


def test_l_identifiant_de_l_appelant_est_rendu_tel_quel():
    """L'appelant réapparie sur son propre id ; un décalage ferait pointer
    la décision sur le mauvais courriel."""
    out = emails.score_messages([{"id": "StnA6yRWmnW3", "from": "a@b.co", "subject": "s"}])
    assert out["results"][0]["id"] == "StnA6yRWmnW3"


def test_entree_vide_et_entrees_non_dict():
    assert emails.score_messages([])["results"] == []
    assert emails.score_messages(["pas un dict", 42])["results"] == []
