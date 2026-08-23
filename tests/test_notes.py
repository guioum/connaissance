"""Tests de commands/notes : source = export anotes, sonde de fraîcheur."""
import os
import time
from pathlib import Path

import pytest

from connaissance.commands import notes
from connaissance.core import paths


def _note(path: Path, created="2026-08-01") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ncreated: {created}\nmodified: {created}\n---\n# {path.stem}\n",
        encoding="utf-8",
    )


@pytest.fixture
def export(tmp_path, monkeypatch):
    """Un export anotes minimal : marqueur frais + deux notes, miroir vide.

    Le « home » des chemins canoniques (`tracking.BASE_PATH`, posé par la
    fixture autouse) est ``tmp_path`` ; on aligne `notes.BASE_PATH` dessus et
    on neutralise le prérequis de racine pour que ``TrackingDB()`` (ouverte par
    `scan`/`backlog_count` sans ``db``) pointe la DB jetable.
    """
    from connaissance.core import tracking

    src = tmp_path / "Archives" / "Notes"
    dest = tmp_path / "Connaissance" / "Transcriptions" / "Notes"
    src.mkdir(parents=True)
    dest.mkdir(parents=True)
    (src / notes.EXPORT_STATE_FILE).write_text("{}", encoding="utf-8")
    _note(src / "Journal" / "a.md")
    _note(src / "Archives" / "vieille.md")  # dossier ignoré par filtres.yaml
    monkeypatch.setattr(notes, "NOTES_DIR", src)
    monkeypatch.setattr(notes, "TRANSCRIPTIONS_DIR", dest)
    monkeypatch.setattr(notes, "BASE_PATH", tmp_path)
    monkeypatch.setattr(tracking, "require_connaissance_root", lambda: None)
    return src, dest


def _note_full(path: Path, title: str, body: str,
               created="2026-01-02 13:28:58", modified="2026-01-06 03:34:27") -> None:
    """Une note au format réel de l'export anotes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: \"{title}\"\ncreated: {created}\nmodified: {modified}\n"
        f"apple_id: E64FDC08-D082-4D85-A1A8-E0CF3069A5C8\nsource: Apple Notes\n---\n"
        f"\n# {title}\n\n{body}\n",
        encoding="utf-8",
    )


def _organised(path: Path, title: str, body: str) -> None:
    """Une transcription telle que `organize apply` l'a laissée : frontmatter
    réécrit (formes ISO quotées, `date` ajouté), même corps que la note."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: {title}\ncreated: '2026-01-02T13:28:58'\n"
        f"modified: '2026-01-06T03:34:27'\n"
        f"apple_id: E64FDC08-D082-4D85-A1A8-E0CF3069A5C8\nsource: Apple Notes\n"
        f"date: '2026-01-02'\n---\n\n# {title}\n\n{body}\n",
        encoding="utf-8",
    )


@pytest.fixture
def connue(export, tracking_db, tmp_path):
    """Une note de l'export DÉJÀ ingérée et rangée par entité (le miroir
    `Transcriptions/Notes/<rel>` n'existe plus), enregistrée avec la vieille
    convention `Connaissance/Notes/<rel>` de `source_path` et sans hash."""
    src, _ = export
    note = src / "Notes" / "AI en local.md"
    _note_full(note, "AI en local", "Ollama or LangChain.")
    trans = (tmp_path / "Connaissance" / "Transcriptions" / "Notes" / "divers"
             / "ia-locale" / "2026-01-02 outils-et-ressources-pour-ia-locale.md")
    _organised(trans, "AI en local", "Ollama or LangChain.")
    hier = time.time() - 86400   # la transcription date d'hier
    os.utime(trans, (hier, hier))
    tracking_db.register_file(
        str(trans), "transcription", source_type="note",
        source_path="Connaissance/Notes/Notes/AI en local.md",
        created="2026-01-02T13:28:58", modified="2026-01-06T03:34:27",
        mtime=trans.stat().st_mtime)
    return src, note, trans


def test_source_est_l_export_mac_automations():
    """La source d'ingestion est l'export quotidien, plus l'ancien ~/Notes."""
    assert paths.NOTES_EXPORT_DIR == paths.ARCHIVES_ROOT / "Notes"
    assert paths.NOTES_EXPORT_DIR == paths.BASE_PATH / "Archives" / "Notes"
    assert notes.NOTES_DIR == paths.NOTES_EXPORT_DIR


def test_export_status_frais(export):
    src, _ = export
    st = notes.export_status(src)
    assert st["stale"] is False
    assert st["age_days"] == 0
    assert st["last_export"] is not None
    assert st["source"] == str(src)


def test_export_status_perime(export):
    src, _ = export
    vieux = time.time() - (notes.EXPORT_STALE_DAYS + 2) * 86400
    os.utime(src / notes.EXPORT_STATE_FILE, (vieux, vieux))
    st = notes.export_status(src)
    assert st["stale"] is True
    assert st["age_days"] >= notes.EXPORT_STALE_DAYS + 1


def test_export_status_sans_marqueur(tmp_path):
    """Un dossier sans `.export_state.json` n'est pas un export garanti : stale."""
    st = notes.export_status(tmp_path)
    assert st == {"last_export": None, "age_days": None, "stale": True,
                  "source": str(tmp_path)}


def test_backlog_count_rapporte_la_sonde(export):
    src, _ = export
    out = notes.backlog_count()
    assert out["export"]["stale"] is False
    assert out["export"]["source"] == str(src)
    # backlog_count ne lit pas filtres.yaml : les deux notes sont comptées
    assert out["total_to_copy"] == 2


def test_scan_rapporte_la_sonde_et_filtre_les_dossiers_ignores(export):
    out = notes.scan()
    assert out["export"]["stale"] is False
    rels = {it["rel"] for it in out["to_copy"]}
    assert rels == {"Journal/a.md"}
    assert any(s["reason"] for s in out["skipped"])


def test_scan_sans_export_echoue_proprement(tmp_path, monkeypatch):
    """Sans dossier d'export, `scan` refuse (require_paths) plutôt que de
    retourner un backlog vide trompeur."""
    monkeypatch.setattr(notes, "NOTES_DIR", tmp_path / "absent")
    with pytest.raises(SystemExit):
        notes.scan()


def test_dossiers_ignores_sur_chemin_relatif_pas_absolu(tmp_path):
    """Régression : l'export vit sous `~/Archives/Notes/` et `Archives` est un
    dossier ignoré. Le filtre doit regarder les segments relatifs à la racine
    de l'export, sinon toutes les notes sont rejetées."""
    from connaissance.core.filtres import Filtres

    root = tmp_path / "Archives" / "Notes"
    note = root / "Journal" / "a.md"
    _note(note)
    f = Filtres()
    assert "Archives" in f.notes_config.get("dossiers_ignores", [])
    # Sans root : le chemin absolu contient « Archives » → rejeté (comportement
    # hérité, documenté).
    ok, reason = f.filter_note(note, content=note.read_text())
    assert (ok, reason) == (False, "dossier_ignore:Archives")
    # Avec root : seul « Journal/a.md » est examiné → accepté.
    ok, reason = f.filter_note(note, content=note.read_text(), root=root)
    assert ok is True
    # Et un vrai sous-dossier ignoré de l'export reste rejeté.
    vieille = root / "Archives" / "b.md"
    _note(vieille)
    ok, reason = f.filter_note(vieille, content=vieille.read_text(), root=root)
    assert (ok, reason) == (False, "dossier_ignore:Archives")


# --- Déjà copiée ? la vérité est dans tracking.db, pas dans le miroir ---


def test_note_rel_from_source_toutes_conventions(export, tmp_path):
    src, _ = export
    f = notes.note_rel_from_source
    assert f(str(src / "Journal" / "a.md")) == "Journal/a.md"
    assert f(str(tmp_path / "Notes" / "Journal" / "a.md")) == "Journal/a.md"
    assert f("Connaissance/Notes/Journal/a.md") == "Journal/a.md"
    assert f("Notes/Journal/a.md") == "Journal/a.md"
    assert f("Archives/Notes/Journal/a.md") == "Journal/a.md"
    assert f("Documents/Maison/toiture.pdf") is None
    assert f(None) is None
    # NFD (chemin écrit par un vieil exporteur) → NFC
    assert f("Notes/Re\u0301fe\u0301rence.md") == "R\u00e9f\u00e9rence.md"


def test_parse_frontmatter_dates_avec_heure(tmp_path):
    note = tmp_path / "n.md"
    _note_full(note, "T", "x", created="2026-01-02 13:28:58", modified="2026-01-06 03:34:27")
    d = notes._parse_frontmatter_dates(note.read_text())
    assert d == {"created": "2026-01-02T13:28:58", "modified": "2026-01-06T03:34:27"}
    # forme quotée ISO (transcription organisée) et date seule
    assert notes._parse_frontmatter_dates("---\ncreated: '2026-01-02T13:28:58'\nmodified: 2026-08-01\n---\n") \
        == {"created": "2026-01-02T13:28:58", "modified": "2026-08-01"}


def test_scan_note_connue_rangee_par_entite_est_a_jour(connue, tracking_db):
    """Le miroir a disparu (rangée par entité) mais le corps est identique :
    rien à copier. Avant : « nouveau » → doublon à chaque `notes copy`."""
    src, note, trans = connue
    out = notes.scan(db=tracking_db)
    rels = {it["rel"] for it in out["to_copy"]}
    assert "Notes/AI en local.md" not in rels
    assert any(s["reason"] == "a_jour" for s in out["skipped"])
    # backlog_count : connue et export pas plus récent que l'enregistrement
    bc = notes.backlog_count(db=tracking_db)
    assert bc["known_in_db"] == 1
    assert bc["to_update"] == 0


def test_scan_note_connue_modifiee_cible_l_emplacement_actuel(connue, tracking_db):
    src, note, trans = connue
    _note_full(note, "AI en local", "Ollama or LangChain.\nQwen.\nComfyUI")
    out = notes.scan(db=tracking_db)
    items = {it["rel"]: it for it in out["to_copy"]}
    it = items["Notes/AI en local.md"]
    assert it["status"] == "modifie"
    assert it["tracked"] is True
    assert it["destination"] == str(trans)      # pas le miroir
    assert out["by_status"] == {"modifie": 1, "nouveau": 1}  # + Journal/a.md


def test_copy_modifiee_preserve_le_frontmatter_et_perime_le_resume(connue, tracking_db, tmp_path):
    src, note, trans = connue
    # Un résumé basé sur l'ancienne transcription, enregistré PLUS TARD qu'elle.
    resume = tmp_path / "Connaissance" / "Résumés" / "Notes" / "divers" / "ia-locale" / "x.md"
    resume.parent.mkdir(parents=True)
    resume.write_text("---\nsource_content_hash: vieux\n---\nrésumé", encoding="utf-8")
    tracking_db.register_file(str(resume), "resume", source_type="note",
                              source_path=str(trans), mtime=trans.stat().st_mtime + 10)
    assert tracking_db.stale_resumes() == []

    _note_full(note, "AI en local", "Ollama or LangChain.\nQwen.\nComfyUI",
               modified="2026-08-20 10:00:00")
    out = notes.copy(db=tracking_db)
    assert out["copied"] == 2  # la modifiée + Journal/a.md (nouveau)

    text = trans.read_text(encoding="utf-8")
    fm = notes.parse_frontmatter(text)
    assert "ComfyUI" in text
    assert fm["date"] == "2026-01-02"                 # enrichissement conservé
    assert fm["modified"] == "2026-08-20T10:00:00"    # rafraîchi depuis la note
    assert fm["title"] == "AI en local"
    # Aucune transcription miroir recréée
    assert not (tmp_path / "Connaissance" / "Transcriptions" / "Notes" / "Notes").exists()

    row = tracking_db.get_file(str(trans))
    assert row["hash"] == notes.body_sha256(note.read_text(encoding="utf-8"))
    assert row["modified"] == "2026-08-20T10:00:00"
    # Péremption : préfiltre mtime de `resumes_perimes` voit le résumé.
    stale = tracking_db.stale_resumes()
    assert [r["resume_path"] for r in stale] == ["Connaissance/Résumés/Notes/divers/ia-locale/x.md"]


def test_copy_deux_fois_ne_recopie_pas(connue, tracking_db):
    """Après une mise à jour, le hash enregistré rend la note « à jour »
    sans relire la transcription."""
    src, note, trans = connue
    _note_full(note, "AI en local", "Nouveau corps.")
    notes.copy(db=tracking_db)
    out = notes.scan(db=tracking_db)
    assert "Notes/AI en local.md" not in {it["rel"] for it in out["to_copy"]}


def test_scan_note_connue_transcription_disparue(connue, tracking_db):
    src, note, trans = connue
    trans.unlink()
    out = notes.scan(db=tracking_db)
    it = {i["rel"]: i for i in out["to_copy"]}["Notes/AI en local.md"]
    assert it["status"] == "manquante"
    assert it["destination"] == str(trans)
    notes.copy(db=tracking_db)
    assert trans.exists()
    assert "Ollama" in trans.read_text(encoding="utf-8")


def test_backlog_count_note_connue_export_plus_recent(connue, tracking_db):
    """Sans lire le contenu : export réécrit après l'enregistrement → à mettre
    à jour (approximation assumée)."""
    src, note, trans = connue
    futur = time.time() + 3600
    os.utime(note, (futur, futur))
    bc = notes.backlog_count(db=tracking_db)
    assert bc["to_update"] == 1
    assert bc["to_copy"] == 2  # Journal/a.md + Archives/vieille.md (pas de filtres ici)


def test_scan_ligne_heritee_rendu_different_mais_texte_identique(connue, tracking_db):
    """Transcription héritée (sans hash) issue d'un autre exporteur : seule la
    mise en forme diffère → à jour (compté à part), pas de résumé périmé."""
    src, note, trans = connue
    _organised(trans, "AI en local", "**🚀 Lointain:** Ollama\n[#Actif](../tags/Actif.md)")
    _note_full(note, "AI en local", "🚀 **Lointain:** Ollama\n#Actif")
    out = notes.scan(db=tracking_db)
    assert "Notes/AI en local.md" not in {it["rel"] for it in out["to_copy"]}
    assert {s["reason"]: s["count"] for s in out["skipped"]}.get("a_jour_rendu") == 1


def test_scan_ligne_heritee_vrai_changement_reste_modifiee(connue, tracking_db):
    src, note, trans = connue
    _organised(trans, "AI en local", "- [ ] Valider avec Stéphane.")
    _note_full(note, "AI en local", "- [x] Valider avec Stéphane.")
    out = notes.scan(db=tracking_db)
    it = {i["rel"]: i for i in out["to_copy"]}.get("Notes/AI en local.md")
    assert it is not None and it["status"] == "modifie"


def test_scan_ligne_avec_hash_compare_exactement(connue, tracking_db):
    """Dès qu'un hash est enregistré, la comparaison est exacte (et n'ouvre
    pas la transcription) : une différence de rendu redevient une modification
    — normal, les deux rendus viennent alors du même exporteur."""
    src, note, trans = connue
    from connaissance.core.frontmatter import body_sha256
    tracking_db.register_file(str(trans), "transcription",
                              hash=body_sha256(trans.read_text(encoding="utf-8")))
    _note_full(note, "AI en local", "**Ollama** or LangChain.")
    out = notes.scan(db=tracking_db)
    it = {i["rel"]: i for i in out["to_copy"]}.get("Notes/AI en local.md")
    assert it is not None and it["status"] == "modifie"
