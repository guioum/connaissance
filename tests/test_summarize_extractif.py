"""Tests du résumé extractif stdlib (core/summarize_extractif.py)."""
from connaissance.core import summarize_extractif as S


def test_keywords_excludes_stopwords_and_ranks_by_freq():
    txt = ("La facture concerne la facture du loyer. Le loyer est mensuel. "
           "Loyer loyer paiement.")
    kw = S.keywords(txt, top_n=3)
    assert "loyer" in kw          # le plus fréquent
    assert "le" not in kw and "la" not in kw   # mots-vides exclus


def test_sentences_split_on_punct_and_newlines():
    s = S.tokenize_sentences("Bonjour le monde. Voici un test\nLigne suivante")
    assert s == ["Bonjour le monde.", "Voici un test", "Ligne suivante"]


def test_luhn_picks_dense_sentences_in_order():
    text = (
        "Objet du présent contrat de location.\n"
        "Le locataire verse un loyer mensuel de loyer pour le logement loué.\n"
        "Merci.\n"
        "Le bail de location du logement prend effet au mois de mai."
    )
    out = S.luhn_summary(text, max_sentences=2)
    assert len(out) == 2
    assert "Merci." not in out      # phrase pauvre écartée
    # ordre d'origine préservé
    assert out == sorted(out, key=lambda s: text.index(s))


def test_short_text_returns_all_sentences():
    assert S.luhn_summary("Une phrase. Deux.", max_sentences=3) == \
        ["Une phrase.", "Deux."]


def test_entities_amounts_dates_refs():
    txt = ("Facture no FA-2024-0098 émise le 2024-03-15 pour un montant de "
           "1 250,00 $ payable avant le 15/04/2024.")
    ent = S.extract_entities(txt)
    assert any("2024-03-15" in d for d in ent["dates"])
    assert any("FA-2024-0098" in r for r in ent["refs"])
    assert ent["amounts"], "le montant en dollars doit être capté"


def test_summarize_empty_is_safe():
    out = S.summarize("")
    assert out == {"keywords": [], "sentences": [], "entities": {}, "chars": 0}


def test_summarize_full_packet():
    txt = ("Relevé bancaire du compte 12345. Solde de 2 000,00 $ au 2024-01-31. "
           "Dépôt salaire. Retrait guichet automatique. Frais mensuels appliqués.")
    out = S.summarize(txt, max_sentences=2)
    assert out["chars"] > 0
    assert out["keywords"]
    assert 1 <= len(out["sentences"]) <= 2
    assert "amounts" in out["entities"]
