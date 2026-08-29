"""Les compteurs du pipeline respectent la liste d'exclusion du payant.

Régression du 2026-08-29 : `pipeline detect` annonçait 1 029 résumés
manquants (et 5,21 $) là où `summarize prepare` n'en préparait que 144 — les
885 autres étaient sur `exclude-processing.txt` et ne partiraient jamais en
batch. Deux chiffres pour un même lot, dont celui affiché à l'utilisateur
était le faux ; la facture annoncée était 7× la vraie.
"""
from connaissance.commands import pipeline as P
from connaissance.core import filtres


def _exclure(monkeypatch, *rels):
    monkeypatch.setattr(filtres, "load_exclude_set", lambda: set(rels))


def test_is_excluded_source_retire_le_prefixe_documents(monkeypatch):
    _exclure(monkeypatch, "organismes/addison-wesley/the-rails-way.pdf")
    ex = filtres.load_exclude_set()
    # La DB stocke « Documents/<rel> », la liste stocke « <rel> » seul.
    assert filtres.is_excluded_source(
        "Documents/organismes/addison-wesley/the-rails-way.pdf", ex)
    assert filtres.is_excluded_source(
        "organismes/addison-wesley/the-rails-way.pdf", ex)
    assert not filtres.is_excluded_source("organismes/manuvie/relevé.pdf", ex)
    assert not filtres.is_excluded_source(None, ex)


def test_is_excluded_source_compare_en_nfc(monkeypatch):
    """macOS rend NFC et NFD indiscernables à l'ouverture, pas à la
    comparaison : une liste écrite en NFC doit attraper un chemin NFD."""
    nfc = "organismes/hydro-québec/facture.pdf"        # é précomposé
    nfd = "organismes/hydro-québec/facture.pdf"       # e + accent
    _exclure(monkeypatch, nfc)
    assert filtres.is_excluded_source("Documents/" + nfd,
                                      filtres.load_exclude_set())


def test_resumes_manquants_ecarte_les_exclus_et_les_compte(tracking_db,
                                                           monkeypatch):
    _exclure(monkeypatch, "organismes/livres/microservices.pdf")
    monkeypatch.setattr(tracking_db, "missing_resumes", lambda *_a, **_k: [
        {"path": "Transcriptions/Documents/organismes/manuvie/relevé.md",
         "source_type": "document",
         "source_path": "Documents/organismes/manuvie/relevé.pdf"},
        {"path": "Transcriptions/Documents/organismes/livres/microservices.md",
         "source_type": "document",
         "source_path": "Documents/organismes/livres/microservices.pdf"},
    ])

    res = P.resumes_manquants(tracking_db)

    assert res["total"] == 1
    # L'écart n'est pas silencieux : on dit combien ont été mis de côté.
    assert res["exclus"] == 1
    assert res["par_source"] == {"document": 1}
    assert res["fichiers"] == [
        "Transcriptions/Documents/organismes/manuvie/relevé.md"]


def test_estimer_couts_ne_facture_pas_les_exclus(tracking_db, monkeypatch):
    _exclure(monkeypatch, "organismes/livres/microservices.pdf")
    monkeypatch.setattr(tracking_db, "missing_resumes", lambda **_k: [
        {"source_type": "document",
         "source_path": "Documents/organismes/manuvie/relevé.pdf"},
        {"source_type": "document",
         "source_path": "Documents/organismes/livres/microservices.pdf"},
    ])
    monkeypatch.setattr(tracking_db, "stale_synthesis", lambda: [])
    monkeypatch.setattr(P, "moc_perimes", lambda: {"total": 0})

    res = P.estimer_couts(tracking_db, mode="batch")

    assert res["resumes"]["par_source"] == {"document": 1}
    assert res["resumes"]["exclus"] == 1
    # Un seul document facturé (le barème arrondit à 2 décimales).
    assert res["resumes"]["cout"] == round(0.03 * 0.5, 2)


def test_liste_d_exclusion_vide_ne_filtre_rien(tracking_db, monkeypatch):
    """Chemin par défaut (aucune exclusion) : aucun changement de comportement."""
    _exclure(monkeypatch)
    monkeypatch.setattr(tracking_db, "missing_resumes", lambda *_a, **_k: [
        {"path": "a.md", "source_type": "document", "source_path": "Documents/a.pdf"},
    ])
    res = P.resumes_manquants(tracking_db)
    assert res["total"] == 1 and res["exclus"] == 0
