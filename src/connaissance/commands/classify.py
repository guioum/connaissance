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
from connaissance.core.tracking import TrackingDB

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


def known_entities() -> list[str]:
    """Entités déjà classées sur disque (organismes/ + personnes/), dé-sluggées."""
    names: list[str] = []
    for sub in ("organismes", "personnes"):
        d = DOCUMENTS_DIR / sub
        if not d.is_dir():
            continue
        for child in sorted(d.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                names.append(_deslug(child.name))
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
