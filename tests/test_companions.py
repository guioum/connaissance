"""Les compagnons d'un `.md` le suivent quand il bouge.

Régression du 2026-08-30 : `relocate_document` — la primitive de `classify` et
`entities` — déplaçait source + transcription + résumé mais ignorait le JSON
d'annotations et les images d'`Attachments/`. Ces fichiers sont désignés par
des chemins RELATIFS au dossier du `.md` : les laisser derrière ne fait pas
échouer le déplacement, ça casse les liens en silence. Mesuré sur la vraie
base : 1 210 JSON orphelins, 5 896 images non référencées, dont 848 laissés
par les runs `classify`.
"""
import json

from connaissance.core.companions import (annotations_path, companion_moves,
                                          referenced_attachments)


def _md(dossier, nom, corps="", images=None):
    """Un `.md` avec, en option, son JSON d'annotations et ses images."""
    dossier.mkdir(parents=True, exist_ok=True)
    p = dossier / f"{nom}.md"
    p.write_text(f"---\ntitle: {nom}\n---\n{corps}\n", encoding="utf-8")
    if images:
        att = dossier / "Attachments"
        att.mkdir(exist_ok=True)
        for img in images:
            (att / img).write_bytes(b"\xff\xd8fake-jpeg")
        annotations_path(p).write_text(json.dumps(
            [{"id": i, "path": f"./Attachments/{i}"} for i in images]),
            encoding="utf-8")
    return p


def test_annotations_suit_et_prend_le_nouveau_nom(tmp_path):
    """Le JSON s'apparie à son `.md` par le NOM. S'il gardait l'ancien stem
    après un renommage, il ne serait plus jamais retrouvé."""
    src = _md(tmp_path / "vrac", "IMG_4021.jpeg", images=["a.jpg"])
    dst = tmp_path / "organismes" / "hydro" / "2024-01-05 facture.md"

    moves = companion_moves(src, dst)

    ann = [(s, d) for s, d, _ in moves if s.name.endswith("_annotations.json")]
    assert len(ann) == 1
    assert ann[0][0].name == "IMG_4021.jpeg_annotations.json"
    assert ann[0][1].name == "2024-01-05 facture_annotations.json"
    assert ann[0][1].parent == dst.parent


def test_images_du_json_suivent_meme_absentes_du_corps(tmp_path):
    """Le corps markdown ne cite pas toujours les images que le JSON décrit.
    Ne lire que le corps abandonnerait celles-là."""
    src = _md(tmp_path / "vrac", "doc", corps="Aucune image ici.",
              images=["x.jpg", "y.jpg"])
    dst = tmp_path / "rangé" / "doc.md"

    assert referenced_attachments(src) == {"x.jpg", "y.jpg"}
    noms = {s.name for s, _, _ in companion_moves(src, dst)}
    assert {"x.jpg", "y.jpg"} <= noms


def test_image_citee_par_le_corps_seul_suit_aussi(tmp_path):
    """Symétrique : un corps réécrit peut citer une image que le JSON ne
    décrit plus. L'union des deux faces est la seule règle sûre."""
    d = tmp_path / "vrac"
    src = _md(d, "doc", corps="![vue](./Attachments/orpheline.jpg)")
    (d / "Attachments").mkdir(exist_ok=True)
    (d / "Attachments" / "orpheline.jpg").write_bytes(b"x")

    noms = {s.name for s, _, _ in companion_moves(src, tmp_path / "out" / "doc.md")}
    assert "orpheline.jpg" in noms


def test_image_partagee_est_signalee_pour_copie(tmp_path):
    """Deux `.md` du même dossier peuvent citer la même image. La déplacer
    casserait le rendu de celui qui reste — d'où le drapeau `shared`."""
    d = tmp_path / "vrac"
    src = _md(d, "un", images=["commune.jpg"])
    _md(d, "deux", corps="![x](./Attachments/commune.jpg)")

    moves = companion_moves(src, tmp_path / "out" / "un.md")
    partages = {s.name: partage for s, _, partage in moves}
    assert partages["commune.jpg"] is True


def test_image_non_partagee_est_deplacee(tmp_path):
    d = tmp_path / "vrac"
    src = _md(d, "un", images=["propre.jpg"])
    _md(d, "deux", corps="Rien à voir.")

    moves = companion_moves(src, tmp_path / "out" / "un.md")
    partages = {s.name: partage for s, _, partage in moves}
    assert partages["propre.jpg"] is False


def test_json_illisible_n_empeche_pas_le_deplacement(tmp_path):
    """Le JSON est écrit par un modèle : il peut être tronqué. Un compagnon
    illisible doit suivre quand même, pas bloquer le `.md`."""
    src = _md(tmp_path / "vrac", "doc")
    annotations_path(src).write_text("{ tronqué…", encoding="utf-8")

    moves = companion_moves(src, tmp_path / "out" / "doc.md")

    assert referenced_attachments(src) == set()
    assert [s.name for s, _, _ in moves] == ["doc_annotations.json"]


def test_lecture_a_destination_quand_le_md_a_deja_bouge(tmp_path):
    """`relocate_document` déplace le graphe AVANT ses compagnons : à ce
    moment le `.md` n'est plus à la source. Les références doivent alors être
    lues à destination, sinon aucun compagnon ne serait jamais trouvé."""
    d = tmp_path / "vrac"
    d.mkdir()
    (d / "Attachments").mkdir()
    (d / "Attachments" / "img.jpg").write_bytes(b"x")
    (d / "doc_annotations.json").write_text(
        json.dumps([{"id": "img.jpg", "path": "./Attachments/img.jpg"}]),
        encoding="utf-8")
    # Le .md est DÉJÀ à destination ; il n'existe plus à la source.
    dst = _md(tmp_path / "out", "doc", corps="![x](./Attachments/img.jpg)")
    src = d / "doc.md"
    assert not src.exists()

    noms = {s.name for s, _, _ in companion_moves(src, dst)}
    assert noms == {"doc_annotations.json", "img.jpg"}


def test_aucun_compagnon_quand_il_n_y_en_a_pas(tmp_path):
    src = _md(tmp_path / "vrac", "seul")
    assert companion_moves(src, tmp_path / "out" / "seul.md") == []


# --- Réunion des orphelins via le ledger ---

def test_reunir_orphelins_retrouve_le_md_via_le_ledger(tmp_path, monkeypatch,
                                                       tracking_db):
    """Le ledger est la SEULE clé fiable pour réapparier un compagnon.

    Le nom du JSON encode le stem d'alors ; la DB, elle, ne garde que la
    position actuelle et a perdu le lien avec l'emplacement d'origine — d'où
    18 réappariements sur 1 210 par la DB, contre 1 196 par le ledger.
    """
    from connaissance.commands import audit_attachments as A
    from connaissance.core import ledger as L
    T = tmp_path / "Transcriptions"
    vrac, range_ = T / "Documents" / "vrac", T / "Documents" / "organismes" / "hydro"
    vrac.mkdir(parents=True)
    range_.mkdir(parents=True)
    # Le .md a déjà été déplacé (et renommé) ; ses compagnons sont restés.
    (range_ / "2024-01-05 facture.md").write_text(
        "---\nt: 1\n---\n![v](./Attachments/img.jpg)\n", encoding="utf-8")
    (vrac / "IMG_9_annotations.json").write_text(
        json.dumps([{"id": "img.jpg", "path": "./Attachments/img.jpg"}]),
        encoding="utf-8")
    (vrac / "Attachments").mkdir()
    (vrac / "Attachments" / "img.jpg").write_bytes(b"image")
    monkeypatch.setattr(A, "TRANSCRIPTIONS", T)
    # Le ledger porte la trace du déplacement d'origine.
    tracking_db.ledger_record({
        "run_id": L.new_run_id("classify"), "op": "move",
        "old_path": str(vrac / "IMG_9.md"),
        "new_path": str(range_ / "2024-01-05 facture.md"),
        "reason": "classify transcription"})

    plan = A.reunir_orphelins(dry_run=True, db=tracking_db)
    assert plan["orphelins"] == 1 and plan["reunis"] == 1
    assert plan["fichiers_deplaces"] == 2      # le JSON + l'image
    assert (vrac / "IMG_9_annotations.json").exists()   # dry-run : rien bougé

    res = A.reunir_orphelins(dry_run=False, db=tracking_db)
    assert res["reunis"] == 1
    # Le JSON prend le stem du .md d'arrivée, sinon il resterait introuvable.
    assert (range_ / "2024-01-05 facture_annotations.json").exists()
    assert (range_ / "Attachments" / "img.jpg").read_bytes() == b"image"
    assert not (vrac / "IMG_9_annotations.json").exists()


def test_reunir_orphelins_laisse_ce_qu_il_ne_sait_pas_replacer(tmp_path,
                                                               monkeypatch,
                                                               tracking_db):
    """Sans trace dans le ledger, on ne devine pas : compté irrécupérable et
    laissé en place, jamais déplacé au hasard."""
    from connaissance.commands import audit_attachments as A
    T = tmp_path / "Transcriptions"
    vrac = T / "Documents" / "vrac"
    vrac.mkdir(parents=True)
    (vrac / "inconnu_annotations.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(A, "TRANSCRIPTIONS", T)

    res = A.reunir_orphelins(dry_run=False, db=tracking_db)

    assert res["orphelins"] == 1 and res["reunis"] == 0
    assert res["irrecuperables"] == 1
    assert (vrac / "inconnu_annotations.json").exists()
