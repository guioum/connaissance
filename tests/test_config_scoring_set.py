"""Atomes de `config scoring-set` : `add_pattern_marketing` (regex d'adresse)."""
from connaissance.commands import config as config_cmd
from connaissance.core import filtres


def _isole(tmp_path, monkeypatch):
    user = tmp_path / "scoring-courriels.yaml"
    user.write_text(
        "seuils:\n  capturer: 0\n  ignorer: -1\npoids:\n  adresse_marketing: -5\n"
        "  sous_domaine_marketing: -1\n  domaine_personnel: 2\n"
        "domaines_marketing: []\npatterns_marketing: []\n"
        "patterns_sujet_promotionnel: []\nseuils_numeriques:\n  corps_min: 0\n",
        encoding="utf-8")
    monkeypatch.setattr(config_cmd, "USER_SCORING", user)
    monkeypatch.setattr(config_cmd, "require_connaissance_root", lambda: None, raising=False)
    return user


def test_add_pattern_marketing_cible_une_adresse_pas_le_domaine(tmp_path, monkeypatch):
    user = _isole(tmp_path, monkeypatch)
    out = config_cmd.scoring_set(dry_run=False,
                                 add_pattern_marketing=[r"^community@buddyboss\.com$"])
    assert out["written"] is True
    assert any(d["key"] == "patterns_marketing" and r"^community@buddyboss\.com$" in d["after"]
               for d in out["diff"])
    assert "community@buddyboss" in user.read_text(encoding="utf-8")

    f = filtres.Filtres(config_path=filtres.TEMPLATE_FILTRES)
    import yaml
    f._scoring_config = yaml.safe_load(user.read_text(encoding="utf-8"))
    promo, _ = f.score_courriel({"from": "community@buddyboss.com", "subject": "x", "body": "y",
                                 "attachments": [], "headers": {}, "folder": ""})
    support, _ = f.score_courriel({"from": "support@buddyboss.com", "subject": "x", "body": "y",
                                   "attachments": [], "headers": {}, "folder": ""})
    assert promo < support   # l'adresse promo pénalisée, le domaine intact


def test_add_pattern_marketing_regex_invalide_rejetee(tmp_path, monkeypatch):
    user = _isole(tmp_path, monkeypatch)
    avant = user.read_text(encoding="utf-8")
    out = config_cmd.scoring_set(dry_run=False, add_pattern_marketing=["^(unclosed"])
    assert out["written"] is False
    assert user.read_text(encoding="utf-8") == avant
