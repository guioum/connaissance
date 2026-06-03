"""Phase C — `classify prepare` : construire les requêtes Batch de classement.

Brique 2 du pré-classement hybride (OFFLINE, gratuit — ne soumet RIEN). Pour
chaque document : paquet de signaux (Phase B) + proposition heuristique
(`core/classify.py`) + liste d'entités connues → une requête pour la Batch API
Anthropic (même pattern que `summarize`). Le skill soumet ensuite via
``mcp__claude_api__submit_batch`` ; ``classify register`` (brique 4) consommera
les résultats.

On envoie **tous** les documents à Claude (input court = peu cher), le
heuristique servant de hint + fallback, jamais de gate.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path

from connaissance.core import classify as _heur
from connaissance.core.output_file import write_or_inline
from connaissance.core.paths import DOCUMENTS_DIR, transit_file
from connaissance.core.resolution import (chercher_alias, construire_nom_fichier,
                                          construire_slug)
from connaissance.core.tracking import TrackingDB

# Taxonomie canonique des catégories (alignée sur prompts/resume_document.md).
CANONICAL_CATEGORIES = {
    "achats", "assurances", "banque", "emplois", "impots", "juridique",
    "logement", "sante", "telecom", "transport", "abonnements", "divers",
}
_DATE_OK = re.compile(r"^\d{4}-\d{2}-\d{2}$")

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"   # suffisant pour un signal court
DEFAULT_MAX_TOKENS = 400
_MAX_KNOWN_ENTITIES = 250


def _load_template() -> tuple[str, str]:
    content = (PROMPTS_DIR / "classify_document.md").read_text(encoding="utf-8")
    parts = re.split(r"<!-- (system|user) -->\n?", content)
    system = user = ""
    for i in range(1, len(parts) - 1, 2):
        if parts[i] == "system":
            system = parts[i + 1].strip()
        elif parts[i] == "user":
            user = parts[i + 1].strip()
    return system, user


def _subst(template: str, variables: dict) -> str:
    def repl(m):
        v = variables.get(m.group(1))
        return str(v) if v not in (None, "", [], {}) else "inconnu"
    return re.sub(r"\{\{(\w+)\}\}", repl, template)


def _deslug(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").strip().title()


def _norm(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", (s or "").lower())
                   if unicodedata.category(c) != "Mn")


# Boilerplate documentaire générique (jamais discriminant) — retiré des
# mots-clés ENVOYÉS à Claude pour les rendre plus précis. (N'affecte pas le
# matching de catégorie du heuristique, qui garde son haystack complet.)
_GENERIC_KW_NOISE = {
    "date", "total", "montant", "somme", "numero", "nom", "prenom", "page",
    "reference", "ref", "adresse", "client", "telephone", "tel", "courriel",
    "email", "www", "com", "http", "https", "inc", "ltee", "ltd",
}


def noise_keyword_tokens() -> set[str]:
    """Tokens à filtrer des mots-clés : boilerplate + noms du FOYER (dérivés de
    ``personnes/`` — ton propre nom sature les documents sans rien discriminer)."""
    toks = set(_GENERIC_KW_NOISE)
    d = DOCUMENTS_DIR / "personnes"
    if d.is_dir():
        for child in d.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                for t in re.split(r"[-_\s]+", child.name):
                    if len(t) >= 3:
                        toks.add(_norm(t))
    return toks


# Noms de dossiers génériques qui ne sont PAS des entités : les exclure de la
# liste connue évite que le LLM y rattache un doc à tort (cf. A/B : « Document »).
_JUNK_ENTITY_NAMES = {
    "document", "documents", "divers", "scan", "scans", "fichier", "fichiers",
    "note", "notes", "autre", "autres", "inconnu", "inconnus", "temp", "tmp",
    "a classer", "a trier", "vrac", "sans titre",
}


def known_entities() -> list[str]:
    """Entités déjà classées sur disque (organismes/ + personnes/), dé-sluggées,
    junk générique exclu."""
    names: list[str] = []
    for sub in ("organismes", "personnes"):
        d = DOCUMENTS_DIR / sub
        if not d.is_dir():
            continue
        for child in sorted(d.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                name = _deslug(child.name)
                if _norm(name) in _JUNK_ENTITY_NAMES:
                    continue
                names.append(name)
    return names[:_MAX_KNOWN_ENTITIES]


def _custom_id(rel: str) -> str:
    return "cls_" + hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]


def _build_request(sig: dict, system: str, user_tpl: str, model: str,
                   known: list[str], noise: set[str]) -> dict:
    hint = _heur.classify(sig, known_entities=known)
    summ = sig.get("summary") or {}
    ent = summ.get("entities") or {}
    dates = sig.get("dates") or {}
    kws = [k for k in (summ.get("keywords") or []) if _norm(k) not in noise]
    variables = {
        "rel": sig.get("rel", ""),
        "origin_folder": sig.get("origin_folder"),
        "type_hint": sig.get("type_hint"),
        "date_name": dates.get("from_name"),
        "date_meta": dates.get("metadata"),
        "date_fs": dates.get("filesystem_created"),
        "title_meta": sig.get("title_meta"),
        "keywords": ", ".join(kws),
        "sentences": " | ".join((summ.get("sentences") or [])[:3]),
        "amounts": ", ".join(ent.get("amounts") or []),
        "dates": ", ".join(ent.get("dates") or []),
        "refs": ", ".join(ent.get("refs") or []),
        "hint_entity": hint["entity"],
        "hint_type": hint["entity_type"],
        "hint_category": hint["category"],
        "hint_date": hint["date"],
        "hint_sujet": hint["sujet"],
        "hint_title": hint["title"],
    }
    return {
        "custom_id": _custom_id(sig.get("rel", "")),
        "system": system,
        "user": _subst(user_tpl, variables),
        "model": model,
        "max_tokens": DEFAULT_MAX_TOKENS,
        "_hint": hint,        # conservé pour fallback au register
        "_rel": sig.get("rel", ""),
    }


def prepare(scope: str | None = None, from_signals: str | None = None,
            model: str = DEFAULT_MODEL, limit: int | None = None,
            output_file: str | None = None, db: TrackingDB | None = None) -> dict:
    """Construire les requêtes Batch de classement (schema ClassifyPrepare).

    Source des signaux : ``from_signals`` (fichier JSON de `documents signals
    --output-file`) si fourni, sinon scan en direct de ``scope``. Écrit les
    requêtes dans un fichier de transit (consommable par le skill / register)
    et retourne un récap + un échantillon de prompts rendus.
    """
    if from_signals:
        payload = json.loads(Path(from_signals).expanduser().read_text(encoding="utf-8"))
        docs = payload.get("documents") or []
    else:
        from connaissance.commands import signals as _signals
        docs = _signals.scan(scope=scope, db=db).get("documents") or []

    if limit is not None:
        docs = docs[:limit]

    system_base, user_tpl = _load_template()
    known = known_entities()
    known_str = ", ".join(known) if known else "(aucune encore)"
    # La liste d'entités connues est IDENTIQUE pour tous les documents : on la
    # met dans le bloc SYSTEM (mis en cache par submit_batch) plutôt que dans
    # chaque user — l'input facturé par requête s'en trouve très réduit.
    system = (system_base
              + "\n\n## Entités connues (aligne-toi dessus si pertinent)\n"
              + known_str)

    noise = noise_keyword_tokens()
    requests = [_build_request(d, system, user_tpl, model, known, noise)
                for d in docs]

    # Fichier de transit (persistant) consommé par submit_batch puis register.
    transit = transit_file("classify")
    transit.write_text(json.dumps({"requests": requests}, ensure_ascii=False),
                       encoding="utf-8")

    payload = {
        "total": len(requests),
        "model": model,
        "transit_file": str(transit),
        "known_entities_count": len(known),
        # Les requêtes complètes (volumineuses) ne repartent que via output_file.
        "requests": requests,
    }

    def _summary(p: dict) -> dict:
        sample = [{"rel": r["_rel"], "hint": r["_hint"],
                   "user": r["user"]} for r in p["requests"][:3]]
        return {
            "total": p["total"],
            "model": p["model"],
            "transit_file": p["transit_file"],
            "known_entities_count": p["known_entities_count"],
            "sample_prompts": sample,
        }

    return write_or_inline(payload, output_file=output_file, summary_fn=_summary)


# --- Brique 4 : register (résultats Batch → manifeste plan→apply) -----------

def _parse_result_content(content: str) -> dict | None:
    """Parser le JSON d'une réponse Claude (tolère un bloc ``` et du texte)."""
    if not content:
        return None
    txt = re.sub(r"```(?:json)?", "", content).strip()
    try:
        return json.loads(txt)
    except (ValueError, TypeError):
        m = re.search(r"\{.*\}", txt, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except (ValueError, TypeError):
                return None
    return None


def _reconcile_entity(name: str, entity_type: str) -> tuple[str, str]:
    """(entity_type, slug) réconciliés contre le registre existant.

    Un alias de fiche (``chercher_alias``) l'emporte (canonique) ; sinon on
    construit le slug et on garde le type proposé (validé)."""
    etype = entity_type if entity_type in ("organismes", "personnes", "divers") else "divers"
    alias = chercher_alias(name) if name else None
    if alias and "/" in alias:
        atype, aslug = alias.split("/", 1)
        return atype, aslug
    return etype, construire_slug(name or "")


def register(results_file: str, from_prepare: str,
             output_file: str | None = None) -> dict:
    """Construire le manifeste de pré-classement (schema ClassifyRegister).

    Consomme les résultats Batch (``results_file`` : ``{results:[{custom_id,
    content}]}``) + le fichier de ``prepare`` (``from_prepare`` : requêtes avec
    ``_rel`` source et ``_hint`` de repli). Pour chaque doc : parse la sortie
    Claude, **valide** (catégorie canonique, date AAAA-MM-JJ), **réconcilie**
    l'entité (``resolution.py``) et calcule la destination. Confiance basse,
    date absente ou parse échoué → **zone d'attente** (pas de déplacement auto).
    **N'écrit/ne déplace rien** : produit un manifeste révisable pour ``apply``.
    """
    results = json.loads(Path(results_file).expanduser().read_text(encoding="utf-8"))
    results = results.get("results", results) if isinstance(results, dict) else results
    prep = json.loads(Path(from_prepare).expanduser().read_text(encoding="utf-8"))
    prep_reqs = prep.get("requests", prep) if isinstance(prep, dict) else prep
    by_id = {r["custom_id"]: r for r in prep_reqs}

    entries: list[dict] = []
    for res in results:
        cid = res.get("custom_id", "")
        req = by_id.get(cid, {})
        source = req.get("_rel", "")
        hint = req.get("_hint", {})
        ext = ("." + source.rsplit(".", 1)[-1]) if "." in source.split("/")[-1] else ""
        j = _parse_result_content(res.get("content", "")) or {}

        entity = (j.get("entity") or "").strip()
        etype_raw = j.get("entity_type") or "divers"
        category = j.get("category") if j.get("category") in CANONICAL_CATEGORIES else None
        date = j.get("date") if isinstance(j.get("date"), str) and _DATE_OK.match(j.get("date") or "") else None
        title = (j.get("title") or "").strip()
        sujet = (j.get("sujet") or "").strip() or None
        confidence = j.get("confidence") if j.get("confidence") in ("high", "low") else "low"

        reasons = []
        if not j:
            reasons.append("parse_échoué")
        if not entity:
            reasons.append("entité_absente")
        if not category:
            reasons.append("catégorie_invalide")
        if not date:
            reasons.append("date_absente")

        etype, slug = _reconcile_entity(entity, etype_raw)
        namefile = construire_nom_fichier(date or "0000-00-00", title or "sans-titre")

        # Statut : auto seulement si confiance haute ET date ET entité ET catégorie.
        auto = (confidence == "high" and date and entity and category
                and etype != "divers" and slug)
        if auto:
            dest = f"{etype}/{slug}/{namefile}{ext}"
            status = "auto"
        else:
            dest = None
            status = "attente"
            if confidence != "high":
                reasons.append("confiance_basse")
            if etype == "divers":
                reasons.append("entité_divers")

        entries.append({
            "custom_id": cid, "source": source, "status": status, "dest": dest,
            "entity": entity or hint.get("entity"), "entity_type": etype,
            "entity_slug": slug, "category": category, "date": date,
            "title": title, "sujet": sujet, "confidence": confidence,
            "reasons": reasons,
        })

    transit = transit_file("classify-manifest")
    transit.write_text(json.dumps({"entries": entries}, ensure_ascii=False),
                       encoding="utf-8")

    auto_n = sum(1 for e in entries if e["status"] == "auto")
    payload = {
        "total": len(entries),
        "auto": auto_n,
        "attente": len(entries) - auto_n,
        "manifest_file": str(transit),
        "entries": entries,
    }

    def _summary(p: dict) -> dict:
        from collections import Counter
        es = p["entries"]
        return {
            "total": p["total"], "auto": p["auto"], "attente": p["attente"],
            "manifest_file": p["manifest_file"],
            "by_entity_type": dict(Counter(e["entity_type"] for e in es).most_common()),
            "by_category": dict(Counter(e["category"] for e in es if e["category"]).most_common()),
            "attente_reasons": dict(Counter(r for e in es for r in e["reasons"]).most_common()),
            "sample_auto": [{"source": e["source"], "dest": e["dest"]}
                            for e in es if e["status"] == "auto"][:8],
        }

    return write_or_inline(payload, output_file=output_file, summary_fn=_summary)
