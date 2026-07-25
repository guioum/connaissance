"""Tests de commands/optimize.py sur arborescence tmp :

- promote : collisions de noms résolues par unique_dest (hash ↔ bon contenu) ;
- promote : PJ au hash déjà connu → skip ;
- dedup : réécriture des .md référents + doublon envoyé en corbeille ledger ;
- _prune_empty_upwards / remove_empty_dirs : jamais au-dessus de
  TRANSCRIPTIONS, .DS_Store tolérés/supprimés, dossiers non vides gardés.
"""
import hashlib

import pytest

import connaissance.core.ledger as ledger_mod
from connaissance.core.tracking import canon_file_path
from connaissance.commands import optimize


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def racines(tmp_path, monkeypatch):
    """Rediriger les constantes module d'optimize (et la corbeille ledger)
    vers tmp_path. Retourne (connaissance, transcriptions, promoted)."""
    connaissance = tmp_path / "Connaissance"
    transcriptions = connaissance / "Transcriptions"
    documents = tmp_path / "Documents"
    promoted = documents / "promus"
    transcriptions.mkdir(parents=True)
    documents.mkdir()
    monkeypatch.setattr(optimize, "CONNAISSANCE", connaissance)
    monkeypatch.setattr(optimize, "TRANSCRIPTIONS", transcriptions)
    monkeypatch.setattr(optimize, "DOCUMENTS_DIR", documents)
    monkeypatch.setattr(optimize, "PROMOTED_DIR", promoted)
    # safe_trash construit la corbeille sous le CONNAISSANCE_ROOT du module
    # ledger (lié par valeur à l'import) : sans ce patch, les tests
    # écriraient dans le vrai ~/Connaissance/.trash/.
    monkeypatch.setattr(ledger_mod, "CONNAISSANCE_ROOT", connaissance)
    return connaissance, transcriptions, promoted


# --- promote ---


def test_promote_deux_pj_meme_nom_contenus_differents(racines, tracking_db):
    _, transcriptions, promoted = racines
    contenu_a = b"%PDF contenu A"
    contenu_b = b"%PDF contenu B, plus long"   # taille différente de A
    pj_a = transcriptions / "Courriels" / "e1" / "Attachments" / "facture.pdf"
    pj_b = transcriptions / "Courriels" / "e2" / "Attachments" / "facture.pdf"
    for pj, contenu in ((pj_a, contenu_a), (pj_b, contenu_b)):
        pj.parent.mkdir(parents=True)
        pj.write_bytes(contenu)

    promus = optimize.promote(tracking_db, dry_run=False)

    assert promus == 2
    # L'ordre de rglob n'est pas garanti : on vérifie l'ENSEMBLE des couples
    # (nom distinct via unique_dest, hash pointant le bon contenu).
    dest_1 = promoted / "facture.pdf"
    dest_2 = promoted / "facture (2).pdf"
    assert dest_1.exists() and dest_2.exists()
    assert {dest_1.read_bytes(), dest_2.read_bytes()} == {contenu_a, contenu_b}
    for dest in (dest_1, dest_2):
        # Chaque hash enregistré pointe le chemin dont il est VRAIMENT le contenu.
        assert tracking_db.has_hash(_sha(dest.read_bytes())) == \
                canon_file_path(dest)
    # Les sources restent en place (promotion = copie).
    assert pj_a.exists() and pj_b.exists()


def test_promote_meme_nom_meme_taille_contenus_differents(racines, tracking_db):
    # Régression corrigée le 2026-07-17 : deux PJ distinctes de MÊME taille —
    # get_or_compute_hash(src) enregistrait le hash de la 2e PJ sous son
    # propre chemin et has_hash() la retrouvait ELLE-MÊME → skip à tort.
    _, transcriptions, promoted = racines
    contenu_a = b"%PDF contenu A"
    contenu_b = b"%PDF contenu B"   # même taille que A, contenu différent
    assert len(contenu_a) == len(contenu_b)
    pj_a = transcriptions / "Courriels" / "e1" / "Attachments" / "facture.pdf"
    pj_b = transcriptions / "Courriels" / "e2" / "Attachments" / "facture.pdf"
    for pj, contenu in ((pj_a, contenu_a), (pj_b, contenu_b)):
        pj.parent.mkdir(parents=True)
        pj.write_bytes(contenu)

    promus = optimize.promote(tracking_db, dry_run=False)

    assert promus == 2   # comportement ATTENDU : les deux contenus promus
    dest_1 = promoted / "facture.pdf"
    dest_2 = promoted / "facture (2).pdf"
    assert dest_1.exists() and dest_2.exists()
    assert {dest_1.read_bytes(), dest_2.read_bytes()} == {contenu_a, contenu_b}


def test_promote_hash_deja_connu_skip(racines, tracking_db):
    _, transcriptions, promoted = racines
    contenu = b"%PDF deja connu"
    pj = transcriptions / "Courriels" / "e1" / "Attachments" / "recu.pdf"
    pj.parent.mkdir(parents=True)
    pj.write_bytes(contenu)
    # Le même contenu est déjà indexé ailleurs (ex. document existant).
    deja = transcriptions.parent.parent / "Documents" / "original.pdf"
    deja.write_bytes(contenu)
    tracking_db.register_hash(_sha(contenu), str(deja),
                              size=len(contenu), mtime=deja.stat().st_mtime)

    promus = optimize.promote(tracking_db, dry_run=False)

    assert promus == 0
    assert not (promoted / "recu.pdf").exists()


def test_promote_dry_run_ne_copie_rien(racines, tracking_db):
    _, transcriptions, promoted = racines
    pj = transcriptions / "Courriels" / "e1" / "Attachments" / "doc.pdf"
    pj.parent.mkdir(parents=True)
    pj.write_bytes(b"%PDF x")

    promus = optimize.promote(tracking_db, dry_run=True)

    assert promus == 1
    assert not promoted.exists()


# --- dedup : réécriture des références + corbeille ---


def test_dedup_reecrit_les_references_et_corbeille(racines, tracking_db):
    connaissance, transcriptions, _ = racines
    contenu = b"%PDF contenu duplique"
    keeper = connaissance.parent / "Documents" / "garde.pdf"
    keeper.write_bytes(contenu)
    tracking_db.register_hash(_sha(contenu), str(keeper),
                              size=len(contenu), mtime=keeper.stat().st_mtime)

    dossier = transcriptions / "Courriels" / "fil-x"
    dup = dossier / "Attachments" / "piece.pdf"
    dup.parent.mkdir(parents=True)
    dup.write_bytes(contenu)
    md = dossier / "courriel.md"
    md.write_text(
        "---\ntitle: t\n---\nVoir la PJ [piece.pdf](Attachments/piece.pdf).\n",
        encoding="utf-8")

    run = ledger_mod.new_run_id("optimize")
    removed = optimize.dedup(tracking_db, dry_run=False, run_id=run)

    assert removed == 1
    # Le .md ne pointe plus vers Attachments/ mais vers le keeper.
    contenu_md = md.read_text(encoding="utf-8")
    assert "(Attachments/piece.pdf)" not in contenu_md
    from connaissance.core.tracking import canon_file_path
    assert f"voir {canon_file_path(keeper)}" in contenu_md
    assert _sha(contenu)[:12] in contenu_md
    # Le doublon est en corbeille ledger (réversible), pas supprimé.
    assert not dup.exists()
    ops = tracking_db.ledger_ops(run, status="applied")
    assert len(ops) == 1 and ops[0]["op"] == "trash"
    trashed = connaissance / ".trash" / run / "Transcriptions/Courriels/fil-x/Attachments/piece.pdf"
    assert trashed.read_bytes() == contenu


def test_dedup_dry_run_ne_touche_rien(racines, tracking_db):
    connaissance, transcriptions, _ = racines
    contenu = b"%PDF dup dry"
    keeper = connaissance.parent / "Documents" / "garde.pdf"
    keeper.write_bytes(contenu)
    tracking_db.register_hash(_sha(contenu), str(keeper),
                              size=len(contenu), mtime=keeper.stat().st_mtime)
    dup = transcriptions / "Courriels" / "x" / "Attachments" / "p.pdf"
    dup.parent.mkdir(parents=True)
    dup.write_bytes(contenu)

    removed = optimize.dedup(tracking_db, dry_run=True)

    assert removed == 1          # rapporté…
    assert dup.exists()          # …mais rien déplacé
    assert tracking_db.ledger_runs() == []


# --- dossiers vides ---


def test_prune_empty_upwards_sarrete_a_transcriptions(racines):
    _, transcriptions, _ = racines
    att = transcriptions / "Courriels" / "entite" / "Attachments"
    att.mkdir(parents=True)
    (att / ".DS_Store").write_bytes(b"junk")

    removed = optimize._prune_empty_upwards(att)

    # Attachments, entite et Courriels supprimés ; TRANSCRIPTIONS jamais.
    assert removed == 3
    assert not (transcriptions / "Courriels").exists()
    assert transcriptions.exists()


def test_prune_empty_upwards_garde_les_dossiers_non_vides(racines):
    _, transcriptions, _ = racines
    dossier = transcriptions / "Courriels" / "entite"
    att = dossier / "Attachments"
    att.mkdir(parents=True)
    reste = dossier / "note.md"
    reste.write_text("contenu", encoding="utf-8")

    removed = optimize._prune_empty_upwards(att)

    assert removed == 1                  # seulement Attachments/
    assert dossier.exists() and reste.exists()


def test_prune_empty_upwards_sur_transcriptions_est_un_noop(racines):
    _, transcriptions, _ = racines
    assert optimize._prune_empty_upwards(transcriptions) == 0
    assert transcriptions.exists()


def test_remove_empty_dirs_supprime_vides_et_ds_store_seulement(racines):
    _, transcriptions, _ = racines
    # Chaîne de dossiers vides (avec .DS_Store parasites).
    vide = transcriptions / "Notes" / "a" / "b"
    vide.mkdir(parents=True)
    (vide / ".DS_Store").write_bytes(b"junk")
    ((transcriptions / "Notes" / "a") / ".DS_Store").write_bytes(b"junk")
    # Dossier contenant un vrai fichier : intouchable.
    plein = transcriptions / "Courriels" / "garde"
    plein.mkdir(parents=True)
    (plein / "note.md").write_text("x", encoding="utf-8")

    removed = optimize.remove_empty_dirs()

    assert removed == 3                  # b, a, Notes
    assert not (transcriptions / "Notes").exists()
    assert (plein / "note.md").exists()
    assert transcriptions.exists()
