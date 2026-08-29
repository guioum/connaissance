"""Estimation des coûts : calibration empirique depuis llm_usage."""
from connaissance.commands import pipeline as P


def _seed_usage(db, operation, source_type, n, unit_cost, mode="batch"):
    for i in range(n):
        db._conn.execute(
            """INSERT INTO llm_usage (operation, source_type, mode, cost_usd,
                                      input_tokens, output_tokens,
                                      cache_creation_input_tokens,
                                      cache_read_input_tokens, units)
               VALUES (?, ?, ?, ?, 0, 0, 0, 0, 1)""",
            (operation, source_type, mode, unit_cost))
    db._conn.commit()


def test_observed_unit_costs_seuil_echantillons(tracking_db):
    _seed_usage(tracking_db, "resume", "document", 40, 0.005)
    _seed_usage(tracking_db, "resume", "courriel", 5, 0.002)   # < min_samples
    obs = tracking_db.observed_unit_costs(min_samples=30)
    assert obs["resume"]["n"] == 45
    assert "document" in obs["resume"]["par_source"]
    assert "courriel" not in obs["resume"]["par_source"]   # trop peu de points
    assert abs(obs["resume"]["par_source"]["document"]["unit_cost"] - 0.005) < 1e-9


def test_estimer_couts_projette_l_empirique(tracking_db, monkeypatch):
    """L'estimateur expose une projection aux coûts observés à côté du barème
    statique (qui surestimait ~3× : cache + mix Haiku ignorés)."""
    _seed_usage(tracking_db, "resume", "document", 100, 0.005)
    _seed_usage(tracking_db, "synthesis", None, 40, 0.016)
    monkeypatch.setattr(tracking_db, "missing_resumes",
                        lambda since=None, until=None: [
                            {"source_type": "document"}] * 10)
    monkeypatch.setattr(tracking_db, "stale_synthesis", lambda: ["e"] * 5)
    monkeypatch.setattr(P, "moc_perimes", lambda db=None: {"total": 0})

    res = P.estimer_couts(tracking_db, mode="batch")
    # Barème statique inchangé (compat) : 10 docs × 0.03 × 0.5
    assert res["resumes"]["cout"] == 0.15
    # Projection empirique : 10 × 0.005 et 5 × 0.016
    assert res["empirique"]["resumes"] == 0.05
    assert res["empirique"]["synthese"] == 0.08
    assert res["empirique"]["total"] == 0.13
    assert res["empirique"]["calibration"]["resume"]["n"] == 100


def test_estimer_couts_sans_historique_replie_sur_bareme(tracking_db, monkeypatch):
    monkeypatch.setattr(tracking_db, "missing_resumes",
                        lambda since=None, until=None: [
                            {"source_type": "document"}] * 4)
    monkeypatch.setattr(tracking_db, "stale_synthesis", lambda: ["e"] * 2)
    monkeypatch.setattr(P, "moc_perimes", lambda db=None: {"total": 1})
    res = P.estimer_couts(tracking_db, mode="batch")
    assert res["empirique"]["resumes"] == res["resumes"]["cout"]
    assert res["empirique"]["synthese"] == res["synthese"]["cout"]
    assert res["empirique"]["moc"] == res["moc"]["cout"]


# --- Table de tarifs ---

def test_tarifs_des_modeles_routes_sont_connus():
    """Tout modèle que le pipeline peut router DOIT avoir un tarif explicite.

    Un modèle absent retombe silencieusement sur `_DEFAULT_PRICING` : au
    passage à Sonnet 5 ($2/$10), le coût aurait continué d'être journalisé au
    tarif de Sonnet 4.6 ($3/$15), soit +50 % — et l'écart se serait propagé
    dans la calibration empirique de `pipeline costs`.
    """
    from connaissance.core import model_selection as MS
    from connaissance.core.tracking import PRICING_USD_PER_MTOK
    for modele in (MS.MODEL_SONNET, MS.MODEL_HAIKU):
        assert modele in PRICING_USD_PER_MTOK, f"tarif manquant : {modele}"


def test_compute_cost_applique_cache_et_remise_batch():
    """Le write de cache coûte 1,25×, le read 0,10×, et le batch divise tout
    par deux — la composition des trois est ce qui rend le chiffre juste."""
    from connaissance.core.tracking import compute_cost_usd
    usage = {"input_tokens": 1_000_000, "output_tokens": 0,
             "cache_creation_input_tokens": 1_000_000,
             "cache_read_input_tokens": 1_000_000}
    # Sonnet 5 : 2 $/Mtok en entrée → 2 (plein) + 2,5 (write) + 0,2 (read).
    assert compute_cost_usd("claude-sonnet-5", usage) == 4.7
    assert compute_cost_usd("claude-sonnet-5", usage, batch=True) == 2.35


def test_tarif_inconnu_est_signale_une_seule_fois(capsys):
    """Le repli sur le tarif par défaut doit être audible — mais une fois, pas
    à chaque appel (le journal d'un gros batch deviendrait illisible)."""
    from connaissance.core import tracking
    tracking._TARIFS_INCONNUS_SIGNALES.discard("modele-de-demain")
    usage = {"input_tokens": 1000, "output_tokens": 100}
    tracking.compute_cost_usd("modele-de-demain", usage)
    tracking.compute_cost_usd("modele-de-demain", usage)
    err = capsys.readouterr().err
    assert err.count("modele-de-demain") == 1
    assert "tarif inconnu" in err


def test_un_tarif_connu_ne_previent_pas(capsys):
    from connaissance.core.tracking import compute_cost_usd
    compute_cost_usd("claude-haiku-4-5", {"input_tokens": 1000,
                                          "output_tokens": 100})
    assert capsys.readouterr().err == ""


# --- Re-tarification de la calibration empirique ---

def _seed_tokens(db, operation, model, n, inp, out, mode="batch"):
    from connaissance.core.tracking import compute_cost_usd
    for _ in range(n):
        cost = compute_cost_usd(model, {"input_tokens": inp,
                                        "output_tokens": out},
                                batch=(mode == "batch"))
        db._conn.execute(
            """INSERT INTO llm_usage (operation, model, mode, cost_usd,
                                      input_tokens, output_tokens,
                                      cache_creation_input_tokens,
                                      cache_read_input_tokens, units)
               VALUES (?, ?, ?, ?, ?, ?, 0, 0, 1)""",
            (operation, model, mode, cost, inp, out))
    db._conn.commit()


def test_calibration_reprojette_sur_le_modele_courant(tracking_db):
    """Un coût observé sur Sonnet 4.6 ($3/$15) doit être reprojeté au tarif de
    Sonnet 5 ($2/$10) — sinon on chiffre une dépense future au tarif d'hier et
    la projection de synthèse dépasse d'un tiers."""
    _seed_tokens(tracking_db, "synthesis", "claude-sonnet-4-6", 40,
                 inp=100_000, out=10_000)

    brut = tracking_db.observed_unit_costs(retarifer=False)["synthesis"]
    courant = tracking_db.observed_unit_costs()["synthesis"]

    # 3.0/15.0 -> 2.0/10.0 : exactement deux tiers, entrée comme sortie.
    assert courant["unit_cost"] == round(brut["unit_cost"] * 2 / 3, 6)
    assert courant["retarifes"] == 40


def test_calibration_corrige_un_tarif_revise_a_nom_constant(tracking_db):
    """Haiku 4.5 était inscrit à $0,8/$4 pour un prix réel de $1/$5. Le nom du
    modèle n'a pas changé : re-tarifer seulement quand l'identifiant bouge
    aurait laissé 25 % d'écart en place sur des dizaines de milliers d'appels."""
    from connaissance.core import tracking
    # Un journal d'époque : coûts calculés à l'ANCIEN tarif Haiku.
    ancien = {"input": 0.8, "output": 4.0}
    vrai = tracking.PRICING_USD_PER_MTOK["claude-haiku-4-5"]
    assert vrai == {"input": 1.0, "output": 5.0}, "tarif Haiku officiel"
    for _ in range(40):
        cost = (100_000 * ancien["input"] + 10_000 * ancien["output"]) / 1e6 * 0.5
        tracking_db._conn.execute(
            """INSERT INTO llm_usage (operation, model, mode, cost_usd,
                                      input_tokens, output_tokens,
                                      cache_creation_input_tokens,
                                      cache_read_input_tokens, units)
               VALUES ('classify', 'claude-haiku-4-5', 'batch', ?, 100000,
                       10000, 0, 0, 1)""", (cost,))
    tracking_db._conn.commit()

    courant = tracking_db.observed_unit_costs()["classify"]
    attendu = (100_000 * 1.0 + 10_000 * 5.0) / 1e6 * 0.5
    assert courant["unit_cost"] == round(attendu, 6)
    assert courant["retarifes"] == 40


def test_calibration_ne_retarife_pas_ce_qui_n_a_pas_de_tokens(tracking_db):
    """L'OCR Mistral est facturé à la PAGE : il n'a pas de tokens, son coût
    journalisé est déjà le prix courant et doit rester intact."""
    for _ in range(40):
        tracking_db._conn.execute(
            """INSERT INTO llm_usage (operation, model, mode, cost_usd,
                                      input_tokens, output_tokens,
                                      cache_creation_input_tokens,
                                      cache_read_input_tokens, units)
               VALUES ('ocr_mistral', 'mistral-ocr-4-0', 'batch', 0.004,
                       0, 0, 0, 0, 2)""")
    tracking_db._conn.commit()

    obs = tracking_db.observed_unit_costs()["ocr_mistral"]
    assert obs["unit_cost"] == 0.004 and obs["retarifes"] == 0


def test_modele_courant_du_palier(tracking_db):
    from connaissance.core.model_selection import MODEL_HAIKU, MODEL_SONNET
    p = tracking_db._modele_courant_du_palier
    assert p("claude-sonnet-4-6") == MODEL_SONNET
    assert p("claude-haiku-4-5-20251001") == MODEL_HAIKU
    # Hors des deux paliers routés : on ne devine pas de successeur.
    assert p("claude-opus-4-5-20250929") is None
    assert p("mistral-ocr-4-0") is None
    assert p(None) is None
