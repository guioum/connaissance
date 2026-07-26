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
