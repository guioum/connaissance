"""Module commands/synthesis : synthèse et candidats pour fiches/chronologies.

Expose :
- `plan()` : entités et MOC à régénérer (wrapper pipeline.detect).
- `aliases_candidates(entity)` : scan déterministe des alias dans les résumés (NEW).
- `relations_candidates(entity)` : co-mentions via le frontmatter des résumés (NEW).
- `register(rel_path, source_type, source_path)` : enregistre un résumé dans la DB.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import cast

import yaml

from connaissance.core.paths import CONNAISSANCE_ROOT
from connaissance.core.tracking import TrackingDB
from connaissance.core.resolution import construire_slug

RESUMES = CONNAISSANCE_ROOT / "Résumés"
SYNTHESE = CONNAISSANCE_ROOT / "Synthèse"
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Tarif par défaut des requêtes de synthèse. Les fiches sont long-form
# narratif — Sonnet reste le bon défaut ; Haiku est utilisé pour digest/moc
# via l'heuristique centrale.
DEFAULT_SYNTHESIS_MODEL = "claude-sonnet-4-6"
DEFAULT_SYNTHESIS_MAX_TOKENS = 6144


def _parse_frontmatter(content: str) -> dict | None:
    if not content.startswith("---"):
        return None
    end = content.find("\n---", 4)
    if end < 0:
        return None
    try:
        fm = yaml.safe_load(content[4:end]) or {}
    except yaml.YAMLError:
        return None
    return fm if isinstance(fm, dict) else None


def _iter_entity_resumes(entity: str) -> list[tuple[Path, dict]]:
    """Itérer sur les résumés d'une entité (format 'type/slug').

    Retourne la liste [(path, frontmatter_dict)] pour chaque résumé trouvé.
    """
    try:
        entity_type, entity_slug = entity.split("/", 1)
    except ValueError:
        return []

    results: list[tuple[Path, dict]] = []
    if not RESUMES.exists():
        return results

    for source_label in ("Documents", "Courriels", "Notes"):
        entity_dir = RESUMES / source_label / entity_type / entity_slug
        if not entity_dir.exists():
            continue
        for md_file in entity_dir.rglob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8")
            except OSError:
                continue
            fm = _parse_frontmatter(content)
            if fm is None:
                continue
            results.append((md_file, fm))
    return results


def _load_existing_aliases(entity: str) -> list[str]:
    try:
        entity_type, entity_slug = entity.split("/", 1)
    except ValueError:
        return []
    fiche_path = SYNTHESE / entity_type / entity_slug / "fiche.md"
    if not fiche_path.exists():
        return []
    try:
        fm = _parse_frontmatter(fiche_path.read_text(encoding="utf-8"))
    except OSError:
        return []
    if not fm:
        return []
    return [str(a) for a in (fm.get("aliases") or [])]


# --- API publique ---


def plan(db: TrackingDB | None = None) -> dict:
    """Lister les entités et MOC à régénérer (schema SynthesisPlan)."""
    from connaissance.commands import pipeline
    result = pipeline.detect(db=db, steps=["synthese_perimee", "moc_perimes"])
    return {
        "stale_entities": result.get("synthese_perimee", {}).get("entites", []),
        "stale_mocs": result.get("moc_perimes", {}).get("categories", []),
    }


def aliases_candidates(entity: str) -> dict:
    """Extraire les alias candidats pour une entité (schema AliasesCandidates, NEW).

    Scanne tous les résumés de l'entité et extrait :
    - les valeurs `from` (courriels) — candidats alias d'adresse email
    - les valeurs `entity_name` (documents/notes) — candidats alias de nom
    - les domaines extraits des adresses — candidats alias `*@domain`

    Chaque candidat est scoré par le nombre de résumés où il apparaît.
    Un support ≥ 2 peut être auto-accepté par Claude.
    """
    existing = set(_load_existing_aliases(entity))
    existing_lower = {a.lower() for a in existing}

    candidate_sources: dict[str, dict] = {}  # key_lower → {alias, kind, count}

    for _path, fm in _iter_entity_resumes(entity):
        entity_name = fm.get("entity_name")
        from_field = fm.get("from") or ""

        if entity_name:
            key = str(entity_name).strip().lower()
            if key and key not in existing_lower:
                entry = candidate_sources.setdefault(
                    key, {"alias": str(entity_name).strip(), "kind": "name", "count": 0})
                entry["count"] += 1

        if from_field:
            addr = str(from_field).strip().lower()
            if addr and addr not in existing_lower:
                entry = candidate_sources.setdefault(
                    addr, {"alias": str(from_field).strip(), "kind": "from", "count": 0})
                entry["count"] += 1
            if "@" in addr:
                domain = addr.rsplit("@", 1)[-1].rstrip(">").strip()
                if domain:
                    wildcard = f"*@{domain}"
                    if wildcard.lower() not in existing_lower:
                        entry = candidate_sources.setdefault(
                            wildcard.lower(),
                            {"alias": wildcard, "kind": "domain", "count": 0},
                        )
                        entry["count"] += 1

    candidates = sorted(
        ({"alias": v["alias"], "support_resumes": v["count"], "kind": v["kind"]}
         for v in candidate_sources.values()),
        key=lambda c: (-cast(int, c["support_resumes"]), c["alias"]),
    )

    return {
        "entity": entity,
        "existing_aliases": sorted(existing),
        "candidates": candidates,
    }


def relations_candidates(entity: str) -> dict:
    """Extraire les relations candidates via co-mentions (schema RelationsCandidates, NEW).

    Scanne tous les résumés de l'entité et collecte les autres entités
    mentionnées dans le frontmatter. Une entité co-mentionnée est une
    candidate de relation. Support = nombre de résumés où elle apparaît.
    """
    try:
        entity_type, entity_slug = entity.split("/", 1)
    except ValueError:
        return {"entity": entity, "candidates": []}

    co_mentions: dict[str, list[str]] = defaultdict(list)

    for md_path, fm in _iter_entity_resumes(entity):
        relations = fm.get("relations") or []
        for rel in relations:
            if not isinstance(rel, str) or "/" not in rel:
                continue
            other = rel.strip()
            if other == entity:
                continue
            try:
                rel_path = str(md_path.relative_to(CONNAISSANCE_ROOT))
            except ValueError:
                rel_path = str(md_path)
            co_mentions[other].append(rel_path)

    candidates = []
    for other, supports in co_mentions.items():
        candidates.append({
            "other": other,
            "co_mentions": len(supports),
            "support_resumes": supports,
        })
    candidates.sort(key=lambda c: (-cast(int, c["co_mentions"]), c["other"]))

    return {"entity": entity, "candidates": candidates}


def list_all() -> dict:
    """Inventaire complet de la synthèse pour alimenter le dashboard.

    Retourne la liste de toutes les fiches existantes avec leur
    frontmatter parsé (slug, aliases, status, first-contact, last-contact,
    relations), plus les MOC, les digests, et les sujets. Évite les
    problèmes de normalisation Unicode (NFC vs NFD) quand la skill doit
    itérer sur ``Synthèse/`` : Python lit le système de fichiers
    directement sans passer par un matcher glob qui compare octet par
    octet.

    Retourne ``{personnes: [...], organismes: [...], sujets: [...],
    digests: [...]}``.
    """
    out: dict = {
        "personnes": [],
        "organismes": [],
        "sujets": [],
        "digests": [],
    }
    if not SYNTHESE.exists():
        return out

    for etype in ("personnes", "organismes"):
        type_dir = SYNTHESE / etype
        if not type_dir.is_dir():
            continue
        for entity_dir in sorted(type_dir.iterdir()):
            if not entity_dir.is_dir():
                continue
            fiche = entity_dir / "fiche.md"
            if not fiche.is_file():
                continue
            try:
                content = fiche.read_text(encoding="utf-8")
            except OSError:
                continue
            fm = _parse_frontmatter(content) or {}
            rel = fiche.relative_to(CONNAISSANCE_ROOT)

            def _iso(v):
                if v is None:
                    return None
                iso = getattr(v, "isoformat", None)
                return iso() if callable(iso) else v

            item = {
                "type": etype,
                "slug": fm.get("slug") or entity_dir.name,
                "entity_slug": entity_dir.name,
                "aliases": fm.get("aliases") or [],
                "status": fm.get("status"),
                "first-contact": _iso(fm.get("first-contact")),
                "last-contact": _iso(fm.get("last-contact")),
                "relations": fm.get("relations") or [],
                "fiche_path": str(rel),
                "chronologie_exists": (entity_dir / "chronologie.md").is_file(),
            }
            out[etype].append(item)

    sujets_dir = SYNTHESE / "sujets"
    if sujets_dir.is_dir():
        for moc in sorted(sujets_dir.glob("*.md")):
            try:
                content = moc.read_text(encoding="utf-8")
            except OSError:
                continue
            fm = _parse_frontmatter(content) or {}
            updated_raw = fm.get("updated")
            iso = getattr(updated_raw, "isoformat", None)
            updated = iso() if callable(iso) else updated_raw
            out["sujets"].append({
                "slug": moc.stem,
                "category": fm.get("category") or moc.stem,
                "updated": updated,
                "moc_path": str(moc.relative_to(CONNAISSANCE_ROOT)),
            })

    digests_dir = SYNTHESE / "rapports" / "digests"
    if digests_dir.is_dir():
        digests = sorted(digests_dir.glob("*.md"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        for d in digests:
            out["digests"].append({
                "date": d.stem,
                "path": str(d.relative_to(CONNAISSANCE_ROOT)),
                "mtime": d.stat().st_mtime,
            })

    return out


def entity_paths(entity: str) -> dict:
    """Retourner les chemins canoniques des résumés d'une entité.

    Scanne ``Résumés/{Documents,Courriels,Notes}/{entity_type}/{entity_slug}/``
    et ne retourne que les dossiers qui existent — pas d'invention de
    chemins par le LLM. Utilisé par la skill ``synthetiser`` pour alimenter
    la section « Liens » des fiches de façon déterministe.

    Retourne : ``{entity, paths: [{source, rel_path, count}]}`` où
    ``rel_path`` est toujours relatif à ``~/Connaissance/`` avec la
    capitalisation exacte (``Résumés/Documents/organismes/…``).
    """
    try:
        entity_type, entity_slug = entity.split("/", 1)
    except ValueError:
        return {"entity": entity, "paths": [], "error": "format 'type/slug' attendu"}

    out: list[dict] = []
    for source_label in ("Documents", "Courriels", "Notes"):
        entity_dir = RESUMES / source_label / entity_type / entity_slug
        if not entity_dir.is_dir():
            continue
        count = sum(1 for _ in entity_dir.rglob("*.md"))
        if count == 0:
            continue
        rel = f"Résumés/{source_label}/{entity_type}/{entity_slug}/"
        out.append({
            "source": source_label.lower(),
            "rel_path": rel,
            "count": count,
        })

    return {"entity": entity, "paths": out}


_VALID_KINDS = {"fiche", "chronologie", "moc", "digest", "index"}


# --- Préparation API (batch/direct) -------------------------------------

def _load_prompt_template(name: str) -> tuple[str, str]:
    """Charger un template sectionné `<!-- system -->` / `<!-- user -->`."""
    path = PROMPTS_DIR / name
    content = path.read_text(encoding="utf-8")
    parts = re.split(r"<!-- (system|user) -->\n?", content)
    system_text = ""
    user_text = ""
    i = 1
    while i < len(parts) - 1:
        marker = parts[i]
        body = parts[i + 1] if i + 1 < len(parts) else ""
        if marker == "system":
            system_text = body.strip()
        elif marker == "user":
            user_text = body.strip()
        i += 2
    return system_text, user_text


def _substitute(template: str, variables: dict) -> str:
    def replace(m):
        key = m.group(1).strip()
        return str(variables.get(key, m.group(0)))
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", replace, template)


def _entity_custom_id(entity: str) -> str:
    return hashlib.sha256(f"entity:{entity}".encode()).hexdigest()[:16]


def _read_synthesis_file(entity_type: str, entity_slug: str, name: str) -> str:
    p = SYNTHESE / entity_type / entity_slug / name
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def _compact_resume(fm: dict, body: str, max_body_chars: int = 1200) -> str:
    """Représentation compacte d'un résumé pour l'injection dans un prompt.

    On garde le frontmatter intégralement (métadonnées utiles : date,
    category, entity_name, relations) mais on tronque le body au-delà d'un
    seuil — le LLM n'a pas besoin de tout le narratif pour rédiger une fiche,
    les sections « Informations clés » et « Actions » du résumé suffisent
    dans la grande majorité des cas.
    """
    meta = {k: fm[k] for k in fm
            if k in ("date", "title", "category", "entity_name",
                     "entity_type", "entity_slug", "confidence", "type",
                     "relations")}
    body_trunc = body.strip()
    if len(body_trunc) > max_body_chars:
        body_trunc = body_trunc[:max_body_chars].rsplit("\n", 1)[0] + "\n…"
    meta_block = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True,
                                default_flow_style=False).strip()
    return f"{meta_block}\n---\n{body_trunc}"


def _gather_entity_context(entity: str, max_resumes: int = 40) -> dict:
    """Rassembler le contexte complet pour générer fiche + chronologie.

    Limite volontairement le nombre de résumés injectés pour les entités très
    actives : au-delà de ``max_resumes`` items, on prend les plus récents —
    l'historique ancien est moins discriminant pour la fiche, et la
    chronologie peut rester figée sur les événements passés (append-only
    implicite via la fiche existante).
    """
    try:
        entity_type, entity_slug = entity.split("/", 1)
    except ValueError:
        return {}

    fiche_existante = _read_synthesis_file(entity_type, entity_slug, "fiche.md")
    chronologie_existante = _read_synthesis_file(
        entity_type, entity_slug, "chronologie.md")

    resumes = _iter_entity_resumes(entity)
    # Tri par date frontmatter décroissante ; les items sans date vont en fin.
    def _key(item):
        _p, fm = item
        d = fm.get("date") or fm.get("created") or ""
        return str(d)
    resumes.sort(key=_key, reverse=True)
    resumes = resumes[:max_resumes]

    resumes_blocks: list[str] = []
    for path, fm in resumes:
        try:
            full = path.read_text(encoding="utf-8")
            body = full.split("---", 2)[-1].strip() if full.startswith("---") else full
        except OSError:
            body = ""
        try:
            rel = str(path.relative_to(CONNAISSANCE_ROOT))
        except ValueError:
            rel = str(path)
        resumes_blocks.append(f"### {rel}\n\n{_compact_resume(fm, body)}")

    return {
        "entity": entity,
        "fiche_existante": fiche_existante or "(aucune — première génération)",
        "chronologie_existante": chronologie_existante or "(aucune — première génération)",
        "aliases_candidates": json.dumps(
            aliases_candidates(entity)["candidates"],
            ensure_ascii=False, indent=2),
        "relations_candidates": json.dumps(
            relations_candidates(entity)["candidates"],
            ensure_ascii=False, indent=2),
        "entity_paths": json.dumps(
            entity_paths(entity)["paths"],
            ensure_ascii=False, indent=2),
        "resumes": "\n\n".join(resumes_blocks) if resumes_blocks
                   else "(aucun résumé enregistré pour cette entité)",
        "_resume_count": len(resumes),
    }


def prepare(entities: list[str] | str = "stale",
            preference: str = "auto",
            output_file: str | None = None,
            db: TrackingDB | None = None) -> dict:
    """Construire les requests de synthèse (fiche + chronologie) pour l'API.

    Symétrique de ``summarize.prepare`` : produit un fichier JSON prêt pour
    ``claude_api__submit_batch`` ou ``query_direct``. La rédaction sort de
    la fenêtre du Claude principal — gain double : -50 % de prix en batch,
    et surtout les résumés ne transitent plus par le contexte principal.

    Parameters
    ----------
    entities : list[str] | "stale"
        Format ``type/slug``. ``"stale"`` utilise ``plan()`` pour cibler
        uniquement les entités dont la synthèse est périmée.
    preference : "auto" | "quality" | "economy"
        Route chaque entité via l'heuristique centrale (voir
        ``core.model_selection``). Les fiches/chronologies restent sur
        Sonnet par défaut en mode auto — rédaction narrative ; ``economy``
        bascule sur Haiku si l'utilisateur accepte une qualité moindre
        (utile pour les gros rattrapages).
    """
    if entities == "stale" or entities is None:
        p = plan(db=db)
        target = [f"{e['entity_type']}/{e['entity_slug']}"
                  for e in p.get("stale_entities", [])]
    else:
        target = list(entities)

    from connaissance.core.model_selection import choose_model
    system_tmpl, user_tmpl = _load_prompt_template("synthesis_entity.md")

    requests: list[dict] = []
    total_input_chars = 0
    for entity in target:
        ctx = _gather_entity_context(entity)
        if not ctx:
            continue
        user_text = _substitute(user_tmpl, ctx)
        total_input_chars += len(system_tmpl) + len(user_text)
        choice = choose_model(
            source_type="fiche",
            content_length=len(user_text),
            reference_date=None,
            preference=preference,  # type: ignore[arg-type]
        )
        requests.append({
            "custom_id": _entity_custom_id(entity),
            "system": system_tmpl,
            "user": user_text,
            "model": choice["model"],
            "max_tokens": DEFAULT_SYNTHESIS_MAX_TOKENS,
            "source_type": "fiche_chronologie",
            "entity": entity,
            "model_tier": choice["tier"],
            "model_reason": choice["reason"],
            "resume_count": ctx.get("_resume_count", 0),
        })

    payload = {
        "requests": requests,
        "total": len(requests),
        "estimated_input_tokens": total_input_chars // 4,
        "mode": "direct",  # format du fichier (identique pour batch/direct)
        "preference": preference,
    }

    def _summary(p: dict) -> dict:
        tiers: dict[str, int] = {}
        for r in p["requests"]:
            t = r.get("model_tier", "?")
            tiers[t] = tiers.get(t, 0) + 1
        return {
            "total": p["total"],
            "estimated_input_tokens": p["estimated_input_tokens"],
            "preference": preference,
            "model_tiers": tiers,
        }

    from connaissance.core.output_file import write_or_inline
    return write_or_inline(payload, output_file=output_file, summary_fn=_summary)


def _split_fiche_chronologie(content: str) -> tuple[str, str] | None:
    """Séparer la sortie LLM en (fiche, chronologie) via les marqueurs.

    Retourne ``None`` si un marqueur est manquant — le caller marque alors
    l'entité en erreur et laisse l'humain/LLM retenter.
    """
    fiche_marker = "<!-- FICHE -->"
    chrono_marker = "<!-- CHRONOLOGIE -->"
    stripped = content.strip()

    # Le LLM peut préfixer d'espaces ou d'une ligne vide — strip prudent.
    if fiche_marker not in stripped or chrono_marker not in stripped:
        return None
    _prefix, rest = stripped.split(fiche_marker, 1)
    try:
        fiche, chronologie = rest.split(chrono_marker, 1)
    except ValueError:
        return None
    return fiche.strip(), chronologie.strip()


def register_from_results_file(results_file: str,
                               requests_file: str | None = None,
                               cleanup: bool = True,
                               db: TrackingDB | None = None) -> dict:
    """Enregistrer en masse les fiches+chronologies d'un fichier de résultats API.

    Symétrique de ``summarize.register_from_results_file``. Pour chaque item :
    split du contenu en (fiche, chronologie) via les marqueurs stricts, puis
    double ``register()`` — la paire fiche/chronologie reste atomique.

    ``requests_file`` est requis : les résultats API ne contiennent pas
    ``entity``, on le retrouve via le mapping ``custom_id → entity`` du
    fichier de préparation.
    """
    if db is None:
        db = TrackingDB()

    entity_by_id: dict[str, str] = {}
    model_by_id: dict[str, str] = {}
    if requests_file:
        try:
            rd = json.loads(Path(requests_file).expanduser().read_text(encoding="utf-8"))
            for r in (rd if isinstance(rd, list) else rd.get("requests", [])):
                cid = r.get("custom_id")
                if cid and r.get("entity"):
                    entity_by_id[cid] = r["entity"]
                if cid and r.get("model"):
                    model_by_id[cid] = r["model"]
        except (OSError, json.JSONDecodeError):
            pass

    path = Path(results_file).expanduser()
    if not path.exists():
        return {"registered": 0,
                "errors": [{"error": f"results_file introuvable : {path}"}],
                "paths": []}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return {"registered": 0,
                "errors": [{"error": f"JSON invalide : {e}"}],
                "paths": []}

    items = data if isinstance(data, list) else data.get("results", [])
    if not isinstance(items, list):
        return {"registered": 0,
                "errors": [{"error": "format attendu : [...] ou {results: [...]}"}],
                "paths": []}

    registered = 0
    errors: list[dict] = []
    out_paths: list[dict] = []
    for item in items:
        cid = item.get("custom_id", "")
        raw = item.get("content")
        if isinstance(raw, list):
            content = "".join(
                b.get("text", "") for b in raw
                if isinstance(b, dict) and b.get("type") == "text")
        elif isinstance(raw, str):
            content = raw
        else:
            errors.append({"custom_id": cid, "error": "content manquant"})
            continue
        if not content.strip():
            errors.append({"custom_id": cid, "error": "content vide"})
            continue

        entity = item.get("entity") or entity_by_id.get(cid)
        if not entity:
            errors.append({"custom_id": cid,
                           "error": "entity introuvable — fournir requests_file"})
            continue

        parts = _split_fiche_chronologie(content)
        if parts is None:
            errors.append({"custom_id": cid,
                           "error": "marqueurs FICHE/CHRONOLOGIE manquants"})
            continue
        fiche_md, chrono_md = parts

        rf = register(content=fiche_md, kind="fiche", entity=entity, db=db)
        rc = register(content=chrono_md, kind="chronologie",
                      entity=entity, db=db)
        if rf.get("error") or rc.get("error"):
            errors.append({"custom_id": cid,
                           "error": rf.get("error") or rc.get("error")})
            continue
        registered += 1
        out_paths.append({"custom_id": cid, "entity": entity,
                          "fiche": rf.get("path"),
                          "chronologie": rc.get("path")})

        usage = item.get("usage")
        if isinstance(usage, dict):
            db.log_usage(
                operation="synthesis",
                usage=usage,
                source_type="fiche_chronologie",
                source_path=entity,
                dest_path=rf.get("path"),
                custom_id=cid,
                model=model_by_id.get(cid),
            )

    from connaissance.core.paths import TRANSIT_DIR
    cleaned_up: list[str] = []

    def _is_transit(p: Path) -> bool:
        s = str(p)
        return (
            "/tmp/" in s
            or s.startswith("/var/folders/")
            or s.startswith(str(TRANSIT_DIR))
        )

    if cleanup and not errors:
        try:
            Path(results_file).expanduser().unlink(missing_ok=True)
            cleaned_up.append(str(results_file))
        except OSError:
            pass
        if requests_file:
            rp = Path(requests_file).expanduser()
            if _is_transit(rp):
                try:
                    rp.unlink(missing_ok=True)
                    cleaned_up.append(str(rp))
                except OSError:
                    pass

    return {
        "registered": registered,
        "errors": errors,
        "paths": out_paths,
        "cleaned_up": cleaned_up,
    }


def _synthesis_dest_path(kind: str, entity: str | None) -> Path:
    """Calculer le chemin relatif de destination dans ``Synthèse/``.

    Conventions :
      - fiche/chronologie → ``Synthèse/{entity_type}/{entity_slug}/{kind}.md``
      - moc              → ``Synthèse/sujets/{entity}.md``
      - digest           → ``Synthèse/rapports/digests/{YYYY-MM-DD}.md``
        (``entity`` sert de date ; défaut : aujourd'hui)
      - index            → ``Synthèse/index.md``
    """
    if kind in ("fiche", "chronologie"):
        if not entity or "/" not in entity:
            raise ValueError(
                f"kind={kind} requiert --entity au format 'type/slug' "
                "(ex: 'personnes/jean-dupont')"
            )
        etype, eslug = entity.split("/", 1)
        return Path("Synthèse") / etype / eslug / f"{kind}.md"

    if kind == "moc":
        if not entity:
            raise ValueError("kind=moc requiert --entity (slug de catégorie)")
        return Path("Synthèse") / "sujets" / f"{entity}.md"

    if kind == "digest":
        from datetime import date
        d = entity or date.today().isoformat()
        return Path("Synthèse") / "rapports" / "digests" / f"{d}.md"

    if kind == "index":
        return Path("Synthèse") / "index.md"

    raise ValueError(f"kind inconnu : {kind} (attendu : {sorted(_VALID_KINDS)})")


def register(content: str | None = None,
             kind: str | None = None,
             entity: str | None = None,
             rel_path: str | None = None,
             source_type: str | None = None,
             source_path: str | None = None,
             db: TrackingDB | None = None) -> dict:
    """Écrire une fiche/chronologie/MOC/digest et l'enregistrer dans la DB.

    Deux modes :

    1. **Mode moderne** (recommandé) : passer ``content`` + ``kind`` (+ ``entity``).
       Le chemin de destination est calculé depuis ``kind`` / ``entity`` et
       le fichier est écrit directement sous ``CONNAISSANCE_ROOT / Synthèse/…``.
       Claude n'a jamais à connaître le chemin absolu de la base (important
       pour cowork où le montage VirtioFS diffère entre VM et host).

    2. **Mode hérité** (compat) : passer ``rel_path`` + ``source_type`` +
       ``source_path`` — enregistre uniquement une ligne dans la DB. Conservé
       pour ne pas casser les appels existants ; ne crée aucun fichier.
    """
    if db is None:
        db = TrackingDB()

    # Mode hérité : pas de content → simple enregistrement DB (sans écriture).
    # Inférer `file_type` depuis le chemin pour éviter de tout catégoriser en
    # `"resume"` (ce qui fausse `stale_synthesis` / `missing_resumes` quand
    # on enregistre une fiche ou une chronologie par cette voie).
    if content is None and rel_path:
        rel_lower = rel_path.lower()
        if "synthèse/" in rel_lower or "synthese/" in rel_lower:
            if rel_lower.endswith("/fiche.md"):
                file_type = "fiche"
            elif rel_lower.endswith("/chronologie.md"):
                file_type = "chronologie"
            elif "/_index.md" in rel_lower or rel_lower.endswith("_moc.md"):
                file_type = "moc"
            elif "/digests/" in rel_lower:
                file_type = "digest"
            else:
                file_type = "synthese"
        else:
            file_type = "resume"
        db.register_file(rel_path, file_type,
                         source_type=source_type,
                         source_path=source_path)
        db.log("connaissance", file_type,
               source_type=source_type,
               source_path=source_path,
               dest_path=rel_path)
        return {"registered": 1, "file_type": file_type, "path": rel_path}

    # Mode moderne : écriture + enregistrement.
    if content is None:
        return {
            "error": "content requis (mode moderne) ou rel_path requis (mode hérité)",
        }
    if not kind:
        return {"error": "kind requis quand content est fourni"}
    if kind not in _VALID_KINDS:
        return {"error": f"kind invalide : {kind} (attendu : {sorted(_VALID_KINDS)})"}

    try:
        dest_rel = _synthesis_dest_path(kind, entity)
    except ValueError as e:
        return {"error": str(e)}

    abs_path = CONNAISSANCE_ROOT / dest_rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(content, encoding="utf-8")

    db.register_file(str(dest_rel), kind,
                     source_type=source_type,
                     source_path=source_path)
    db.log("connaissance", "synthese",
           source_type=source_type or kind,
           source_path=source_path,
           dest_path=str(dest_rel))

    return {
        "registered": 1,
        "kind": kind,
        "entity": entity,
        "path": str(dest_rel),
        "abs_path": str(abs_path),
        "bytes": len(content.encode("utf-8")),
    }
