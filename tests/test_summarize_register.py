"""Register des résumés : la vérité de la requête prime sur le
frontmatter recopié par le modèle."""
from pathlib import Path


def test_register_impose_source_de_la_requete(tmp_path, monkeypatch,
                                              tracking_db):
    """La vérité de la REQUÊTE (source_path) prime sur le champ `source`
    recopié par le modèle — qui peut le réécrire (A/B 2026-07-26 : 16-21 %
    de corruption en local, classe d'erreur possible partout). Le fichier
    écrit porte le champ corrigé."""
    from connaissance.commands import summarize as S
    croot = tmp_path / "Connaissance"
    trans = croot / "Transcriptions" / "Documents" / "organismes" / "bn"
    trans.mkdir(parents=True)
    (trans / "releve.md").write_text(
        "---\ncreated: '2024-01-01T00:00:00'\nmodified: '2024-01-01T00:00:00'\n"
        "---\ncorps", encoding="utf-8")
    monkeypatch.setattr(S, "CONNAISSANCE_ROOT", croot)
    contenu = ("---\ntype: document\n"
               "source: Transcriptions/Documents/organismes/CORROMPU/releve.md\n"
               "date: 2024-01-01\ntitle: T\ncategory: banque\n"
               "entity_type: organismes\nentity_slug: bn\nentity_name: BN\n"
               "confidence: high\n---\nRésumé.")
    out = S.register("cid1", contenu,
                     source_path="Transcriptions/Documents/organismes/bn/releve.md",
                     db=tracking_db)
    assert out.get("error") is None or "error" not in out
    p = Path(out["path"]) if not str(out["path"]).startswith("/") else Path(out["path"])
    ecrit = (Path(out["path"]) if Path(out["path"]).is_absolute()
             else croot / out["path"]).read_text(encoding="utf-8")
    assert "organismes/CORROMPU" not in ecrit
    assert "organismes/bn/releve.md" in ecrit


def test_register_ecrit_l_entite_dans_l_index(tmp_path, monkeypatch,
                                              tracking_db):
    """L'entité du frontmatter décide où le fichier est écrit ; elle doit
    donc aussi être dans `files`.

    Régression du 2026-08-29 : 129 résumés d'un batch se sont retrouvés bien
    rangés sur disque mais avec `entity_type NULL` en base. `organize` ne les
    voyait pas (ils étaient déjà à leur place) et `stale_synthesis`, qui
    filtre sur `entity_type IS NOT NULL`, ne les comptait pas pour périmer
    leur fiche : des résumés existants n'atteignaient jamais la synthèse.
    """
    from connaissance.commands import summarize as S
    croot = tmp_path / "Connaissance"
    trans = croot / "Transcriptions" / "Documents" / "organismes" / "bn"
    trans.mkdir(parents=True)
    (trans / "releve.md").write_text("---\n---\ncorps", encoding="utf-8")
    monkeypatch.setattr(S, "CONNAISSANCE_ROOT", croot)
    contenu = ("---\ntype: document\n"
               "source: Transcriptions/Documents/organismes/bn/releve.md\n"
               "date: 2024-01-01\ntitle: T\ncategory: banque\n"
               "entity_type: organismes\nentity_slug: bn\n"
               "entity_name: Banque Nationale\nconfidence: high\n---\nRésumé.")

    out = S.register("cid-entite", contenu,
                     source_path="Transcriptions/Documents/organismes/bn/releve.md",
                     db=tracking_db)

    row = tracking_db.get_file(f"Connaissance/{out['path']}") or \
        tracking_db.get_file(out["path"])
    assert row is not None, "le résumé doit être indexé"
    assert row["entity_type"] == "organismes"
    assert row["entity_slug"] == "bn"


def test_register_derive_le_slug_quand_le_modele_l_omet(tmp_path, monkeypatch,
                                                        tracking_db):
    """`entity_slug` absent du frontmatter : dérivé du nom, jamais laissé vide
    (sinon la ligne reste invisible au regroupement par entité)."""
    from connaissance.commands import summarize as S
    croot = tmp_path / "Connaissance"
    trans = croot / "Transcriptions" / "Documents" / "organismes" / "x"
    trans.mkdir(parents=True)
    (trans / "doc.md").write_text("---\n---\ncorps", encoding="utf-8")
    monkeypatch.setattr(S, "CONNAISSANCE_ROOT", croot)
    contenu = ("---\ntype: document\n"
               "source: Transcriptions/Documents/organismes/x/doc.md\n"
               "date: 2024-01-01\ntitle: T\ncategory: banque\n"
               "entity_type: organismes\nentity_name: Hydro-Québec\n"
               "confidence: high\n---\nRésumé.")

    out = S.register("cid-slug", contenu,
                     source_path="Transcriptions/Documents/organismes/x/doc.md",
                     db=tracking_db)

    row = tracking_db.get_file(f"Connaissance/{out['path']}") or \
        tracking_db.get_file(out["path"])
    assert row is not None and row["entity_slug"] == "hydro-québec"
