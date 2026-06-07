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
from connaissance.core import filtres as _filtres
from connaissance.core import ledger as _ledger
from connaissance.core.manifest_io import load_entries, unique_dest, unwrap
from connaissance.core.output_file import write_or_inline
from connaissance.core.paths import DOCUMENTS_DIR, require_paths, transit_file
from connaissance.core.resolution import (chercher_alias, construire_nom_fichier,
                                          construire_slug, slugify)
from connaissance.core.tracking import TrackingDB

# Taxonomie canonique des catégories — SOURCE UNIQUE, alignée sur
# prompts/_category_rules.md (partagé pré-classement / résumé).
CANONICAL_CATEGORIES = {
    "achats", "assurances", "banque", "emplois", "professionnel", "impots",
    "juridique", "logement", "sante", "telecom", "transport", "abonnements",
    "divers",
}

# Synonymes / fuites (anciens résumés, sorties LLM hors liste) → canonique.
# Les thèmes fins (cuisine, voyages…) retombent sur `divers` : ils vivent dans
# le champ `sujet`, pas dans la catégorie (domaine).
# `finances` est volontairement ABSENT : ambigu (banque vs impots selon le
# contenu — vérifié sur le corpus, les « finances » étaient en fait des impôts).
# Non mappé ⇒ None ⇒ mis en revue plutôt que deviné à tort.
_CATEGORY_SYNONYMS = {
    "santé": "sante", "sante": "sante",
    "voyages": "transport", "voyage": "transport",
    "travail": "professionnel", "professionnelle": "professionnel",
    "emploi": "emplois",
    "cuisine": "divers", "recettes": "divers", "recette": "divers",
    "organisation": "divers", "projets": "divers", "projet": "divers",
    "maison": "divers", "jardin": "divers",
}


def canonicalize_category(cat: str | None) -> str | None:
    """Normaliser une catégorie vers la liste canonique (mappe les synonymes/
    fuites), ou None si vide/inconnue. NFC + minuscules pour matcher « santé »."""
    if not cat:
        return None
    c = unicodedata.normalize("NFC", cat).strip().lower()
    c = _CATEGORY_SYNONYMS.get(c, c)
    return c if c in CANONICAL_CATEGORIES else None


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


# Noms de dossiers génériques qui ne sont PAS des entités : les exclure de la
# liste connue évite que le LLM y rattache un doc à tort (cf. A/B : « Document »).
_JUNK_ENTITY_NAMES = {
    "document", "documents", "divers", "scan", "scans", "fichier", "fichiers",
    "note", "notes", "autre", "autres", "inconnu", "inconnus", "temp", "tmp",
    "a classer", "a trier", "vrac", "sans titre",
}


def _known_entities_from_folders() -> list[str]:
    """Repli : entités déduites des dossiers rangés (organismes/ + personnes/),
    dé-sluggées, junk exclu. Utilisé si le registre `entities` est vide."""
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


def known_entities(db: TrackingDB | None = None) -> list[str]:
    """Liste d'entités connues pour le prompt — depuis le **registre `entities`**
    (canonique + aliases, enrichi de batch en batch). Chaque entrée : « Nom »
    ou « Nom (aussi : alias1, alias2) » pour que le modèle rabatte les variantes.
    Repli sur les dossiers rangés si le registre est vide (premier run)."""
    owns = db is None
    if db is None:
        db = TrackingDB()
    try:
        ents = db.all_entities(limit=_MAX_KNOWN_ENTITIES)
    finally:
        if owns:
            db.close()
    if not ents:
        return _known_entities_from_folders()
    out: list[str] = []
    for e in ents:
        al = e.get("aliases") or []
        out.append(f"{e['name']} (aussi : {', '.join(al)})" if al else e["name"])
    return out


def shared_classification_suffix(db: TrackingDB | None = None,
                                 known: list[str] | None = None) -> str:
    """Bloc système PARTAGÉ par le pré-classement et le classement final (résumé)
    : discipline d'entité (``prompts/_entity_discipline.md``) + règles de catégorie
    (``prompts/_category_rules.md``) + registre d'entités connues (canonique +
    aliases). Source unique de vérité — même entité ET même catégorie dans les
    deux passes. Importé par ``summarize``.
    """
    ent = (PROMPTS_DIR / "_entity_discipline.md").read_text(encoding="utf-8").strip()
    cat = (PROMPTS_DIR / "_category_rules.md").read_text(encoding="utf-8").strip()
    if known is None:
        known = known_entities(db)
    known_str = "\n".join(f"- {k}" for k in known) if known else "(aucune encore)"
    return (ent + "\n\n" + cat
            + "\n\n## Entités connues (aligne-toi dessus si pertinent)\n"
            + known_str)


def _custom_id(rel: str) -> str:
    return "cls_" + hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]


def _build_request(sig: dict, system: str, user_tpl: str, model: str,
                   known: list[str]) -> dict:
    hint = _heur.classify(sig, known_entities=known)
    summ = sig.get("summary") or {}
    ent = summ.get("entities") or {}
    dates = sig.get("dates") or {}
    # Le prompt envoie l'extrait du texte brut (`excerpt`) comme signal premier ;
    # les mots-clés/Luhn du résumé extractif ne sont plus injectés (proxy faible).
    variables = {
        "rel": sig.get("rel", ""),
        "origin_folder": sig.get("origin_folder"),
        "type_hint": sig.get("type_hint"),
        "date_name": dates.get("from_name"),
        "date_meta": dates.get("metadata"),
        "date_fs": dates.get("filesystem_created"),
        "title_meta": sig.get("title_meta"),
        "amounts": ", ".join(ent.get("amounts") or []),
        "dates": ", ".join(ent.get("dates") or []),
        "refs": ", ".join(ent.get("refs") or []),
        "excerpt": sig.get("excerpt")
                   or "(aucun texte extrait — document scanné, image ou non lu)",
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
    known = known_entities(db)
    # Discipline d'entité + règles de catégorie + entités connues = bloc PARTAGÉ
    # avec le classement final (résumé), source unique de vérité. Identique pour
    # tous les documents → reste dans le SYSTEM (caché par submit_batch).
    system = system_base + "\n\n" + shared_classification_suffix(known=known)

    requests = [_build_request(d, system, user_tpl, model, known)
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


# --- Fiche d'identité : vue unifiée (status) --------------------------------

def status(path: str | None = None, db: TrackingDB | None = None) -> dict:
    """Fiche d'identité d'un document (``--path``) ou résumé corpus.

    Avec ``path`` : assemble les étages de la fiche (signaux Phase B +
    classement Phase C + quarantaine secrets) pour ce fichier. Sans :
    compteurs corpus de l'étage classement (statut/catégorie/entité).
    """
    owns_db = db is None
    if db is None:
        db = TrackingDB()
    try:
        if not path:
            return db.classification_summary()
        # Normaliser en chemin relatif à ~/Documents.
        p = Path(path)
        try:
            rel = str(p.relative_to(DOCUMENTS_DIR)) if p.is_absolute() else path
        except ValueError:
            rel = path
        rel = unicodedata.normalize("NFC", rel)
        quarantined = rel in _filtres.load_quarantine_set()
        sig = db.get_signals_row(rel)
        cls = db.get_classification(rel)
        return {
            "rel": rel,
            "found": bool(sig or cls),
            "quarantined": quarantined,
            "signals": sig,
            "classification": cls,
        }
    finally:
        if owns_db:
            db.close()


# --- Brique 5 : apply (manifeste → déplacements ledger, dry-run par défaut) --

def apply(manifest_file: str, dry_run: bool = True,
          db: TrackingDB | None = None) -> dict:
    """Appliquer le manifeste de pré-classement (schema ClassifyApply).

    Déplace chaque entrée ``status=auto`` vers sa destination **via le ledger**
    (``safe_move`` : journalisé, réversible). Les ``attente`` sont laissées en
    place. **Dry-run par défaut** : ne bouge RIEN tant que ``dry_run=False``
    (flag ``--apply``). Collisions de noms gérées (`(2)`, `(3)`…).
    """
    require_paths(DOCUMENTS_DIR, context="classify apply")
    _, entries = load_entries(manifest_file)
    autos = [e for e in entries if e.get("status") == "auto" and e.get("dest")]

    owns_db = db is None
    if db is None:
        db = TrackingDB()
    run_id = _ledger.new_run_id("classify")

    planned: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    try:
        for e in autos:
            src = DOCUMENTS_DIR / e["source"]
            if not src.exists():
                skipped.append({"source": e["source"], "reason": "source_introuvable"})
                continue
            dst = unique_dest(DOCUMENTS_DIR / e["dest"])
            rel_dst = str(dst.relative_to(DOCUMENTS_DIR))
            if dry_run:
                planned.append({"source": e["source"], "dest": rel_dst})
                continue
            try:
                # Ledger + relink de la fiche atomiques : soit les deux sont
                # journalisés, soit aucun (jamais une fiche désynchronisée du
                # ledger). Le shutil.move dans safe_move reste hors-transaction.
                with db.transaction():
                    _ledger.safe_move(db, src, dst,
                                      f"classify {e.get('category') or ''}".strip(),
                                      run_id, commit=False)
                    db.relink_document(e["source"], rel_dst, commit=False)
                planned.append({"source": e["source"], "dest": rel_dst})
            except OSError as exc:
                errors.append({"source": e["source"], "error": str(exc)})
    finally:
        if owns_db:
            db.close()

    result = {
        "dry_run": dry_run,
        "auto_total": len(autos),
        "moved": 0 if dry_run else len(planned),
        "planned": len(planned),
        "attente": sum(1 for e in entries if e.get("status") == "attente"),
        "skipped": skipped,
        "errors": errors,
        "moves": planned[:50],
    }
    if not dry_run and planned:
        result["ledger_run"] = run_id
    return result


# --- Brique 4 : register (résultats Batch → manifeste plan→apply) -----------

def _coerce_content_text(content) -> str:
    """Normaliser le champ ``content`` d'un résultat Batch en texte brut.

    L'API Messages renvoie typiquement une liste de blocs
    ``[{"type": "text", "text": "..."}]`` ; certains exports l'aplatissent en
    chaîne. On accepte les deux (liste de blocs → concat des ``text``, chaîne →
    telle quelle) pour ne pas faire échouer tout le register sur la forme.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text") or block.get("content") or "")
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    if isinstance(content, dict):
        return content.get("text") or content.get("content") or ""
    return ""


def _parse_result_content(content) -> dict | None:
    """Parser le JSON d'une réponse Claude (tolère un bloc ``` et du texte).

    ``content`` peut être une chaîne, une liste de blocs Messages, ou un dict ;
    voir ``_coerce_content_text``.
    """
    content = _coerce_content_text(content)
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
    construit le slug et on garde le type proposé (validé). Un type non
    reconnu retombe sur ``inconnus`` (et non ``divers``) — cohérent avec le
    heuristique ``guess_entity`` et ``organize.py`` : ``divers`` est une vraie
    catégorie de rangement, ``inconnus`` = entité indéterminée."""
    etype = entity_type if entity_type in (
        "organismes", "personnes", "divers", "inconnus") else "inconnus"
    alias = chercher_alias(name) if name else None
    if alias and "/" in alias:
        atype, aslug = alias.split("/", 1)
        return atype, aslug
    return etype, construire_slug(name or "")


def register(results_file: str, from_prepare: str,
             output_file: str | None = None, db: TrackingDB | None = None) -> dict:
    """Construire le manifeste de pré-classement (schema ClassifyRegister).

    Consomme les résultats Batch (``results_file`` : ``{results:[{custom_id,
    content}]}``) + le fichier de ``prepare`` (``from_prepare`` : requêtes avec
    ``_rel`` source et ``_hint`` de repli). Pour chaque doc : parse la sortie
    Claude, **valide** (catégorie canonique, date AAAA-MM-JJ), **réconcilie**
    l'entité (``resolution.py``) et calcule la destination. Une fiche
    **structurellement complète** (type exploitable + entité + catégorie + date)
    passe en **auto** même à confiance basse (le déplacement est réversible via
    le ledger) ; il ne reste en **zone d'attente** que ce qui manque une donnée
    (entité ``divers``/``inconnus``, sans catégorie ou sans date) ou dont le parse
    a échoué. **N'écrit/ne déplace rien** : produit un manifeste révisable pour
    ``apply``.
    """
    results = unwrap(
        json.loads(Path(results_file).expanduser().read_text(encoding="utf-8")),
        "results")
    prep_reqs = unwrap(
        json.loads(Path(from_prepare).expanduser().read_text(encoding="utf-8")),
        "requests")
    by_id = {r["custom_id"]: r for r in prep_reqs}

    owns_db = db is None
    if db is None:
        db = TrackingDB()

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
        category = canonicalize_category(j.get("category"))
        date = j.get("date") if isinstance(j.get("date"), str) and _DATE_OK.match(j.get("date") or "") else None
        title = (j.get("title") or "").strip()
        # Sujet normalisé en slug (minuscules-tirets, ACCENTS CONSERVÉS — même
        # règle que les slugs d'entité) pour éviter les variantes café/cafes.
        sujet = slugify(j.get("sujet") or "") or None
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

        # Aligner sur le registre `entities` : si le nom (ou son slug) matche une
        # entité connue / un alias, réutiliser SON canonique (anti-fragmentation
        # forte) ; sinon réconcilier normalement (nouvelle entité).
        reg = db.resolve_entity(entity) if entity else None
        if reg:
            etype, slug, entity = reg["type"], reg["slug"], reg["name"]
        else:
            etype, slug = _reconcile_entity(entity, etype_raw)
        namefile = construire_nom_fichier(date or "0000-00-00", title or "sans-titre")

        # Statut : auto dès que la fiche est structurellement COMPLÈTE — type
        # d'entité exploitable (ni divers ni inconnus) ET entité ET catégorie ET
        # date ET slug. La confiance basse de Haiku ne bloque PLUS le passage en
        # auto : le déplacement passe par le ledger (réversible), et le modèle se
        # déclare souvent « low » par prudence sur des docs pourtant complets. On
        # garde en attente uniquement ce qui MANQUE une donnée (entité divers/
        # inconnue, sans catégorie ou sans date) ou dont le parsing a échoué.
        auto = (date and entity and category
                and etype not in ("divers", "inconnus") and slug)
        if auto:
            dest = f"{etype}/{slug}/{namefile}{ext}"
            status = "auto"
        else:
            dest = None
            status = "attente"
            if etype in ("divers", "inconnus"):
                reasons.append(f"entité_{etype}")
        # Trace informative : la confiance basse est consignée (dans `reasons` et
        # dans la fiche) mais n'est plus un motif de mise en attente à elle seule.
        if confidence != "high" and "confiance_basse" not in reasons:
            reasons.append("confiance_basse")

        entry = {
            "custom_id": cid, "source": source, "status": status, "dest": dest,
            "entity": entity or hint.get("entity"), "entity_type": etype,
            "entity_slug": slug, "category": category, "date": date,
            "title": title, "sujet": sujet, "confidence": confidence,
            "reasons": reasons,
        }
        entries.append(entry)

        # Persister l'étage classement de la fiche (résumable, interrogeable).
        try:
            st = (DOCUMENTS_DIR / source).stat()
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size = mtime = None
        db.upsert_classification(source, {
            **entry, "model": req.get("model"), "size": size, "mtime": mtime})
        # Registre VIVANT : enrichir `entities` avec l'entité retenue (nouvelle ou
        # +1 compteur). Les batches/tranches suivants s'aligneront dessus.
        if entity and etype in ("organismes", "personnes") and slug:
            db.upsert_entity(etype, slug, entity, inc_count=1)
        # Sujet primaire → table multi-sujet (source 'classify'), pour que la
        # vue « - Sujets » lise une source unique (cf. dédup consciente).
        if sujet:
            db.add_doc_sujets(source, [sujet], "classify")

    if owns_db:
        db.close()

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
            # Motifs d'attente : restreints aux entrées réellement en attente
            # (la confiance basse, désormais informative, peut décorer un auto).
            "attente_reasons": dict(Counter(
                r for e in es if e["status"] == "attente" for r in e["reasons"]
            ).most_common()),
            # Combien d'auto proviennent de l'assouplissement (confiance basse
            # mais fiche complète) — mesure l'effet du changement de porte.
            "auto_low_confidence": sum(
                1 for e in es if e["status"] == "auto" and e["confidence"] != "high"),
            "sample_auto": [{"source": e["source"], "dest": e["dest"]}
                            for e in es if e["status"] == "auto"][:8],
        }

    return write_or_inline(payload, output_file=output_file, summary_fn=_summary)
