"""Dispatcher CLI `connaissance`.

Grammaire : `connaissance <groupe> <verbe> [--flags] [--json|--human]`.

Toutes les sorties sont en JSON par défaut (consommées par le serveur MCP
et les skills). `--human` affiche un texte lisible pour debug terminal.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable


def _json_print(data: Any, human: bool = False) -> None:
    if human:
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, list):
                    print(f"{k}: {len(v)} éléments")
                elif isinstance(v, dict):
                    print(f"{k}: {len(v)} clés")
                else:
                    print(f"{k}: {v}")
            return
        print(data)
        return
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _parse_date_range(args) -> tuple[str | None, str | None]:
    return getattr(args, "since", None), getattr(args, "until", None)


def _cmd_documents(args) -> Any:
    from connaissance.commands import documents
    if args.verb == "scan":
        since, until = _parse_date_range(args)
        return documents.scan(since=since, until=until, output_file=args.output_file)
    if args.verb == "backlog-count":
        since, until = _parse_date_range(args)
        return documents.backlog_count(since=since, until=until)
    if args.verb == "register":
        return documents.register(args.source_file, args.transcription)
    if args.verb == "register-existing":
        return documents.register_existing_all()
    if args.verb == "suspects":
        return documents.suspects()
    if args.verb == "verify-preserve":
        return documents.verify_preserve(args.before, args.after)
    raise SystemExit(f"verbe inconnu : documents {args.verb}")


def _cmd_emails(args) -> Any:
    from connaissance.commands import emails
    since, until = _parse_date_range(args)
    if args.verb == "stats":
        return emails.stats(account=args.account, folder=args.folder,
                            since=since, until=until)
    if args.verb == "backlog-count":
        return emails.backlog_count(account=args.account, folder=args.folder,
                                    since=since, until=until)
    if args.verb == "extract":
        return emails.extract(account=args.account, folder=args.folder,
                              since=since, until=until,
                              dry_run=args.dry_run, no_images=args.no_images)
    if args.verb == "threads":
        return emails.threads(account=args.account, folder=args.folder,
                              since=since, until=until)
    if args.verb == "calibrate":
        return emails.calibrate(sample=args.sample or 200, since=since, until=until,
                                account=args.account)
    if args.verb == "senders":
        return emails.senders(sample=args.sample or 500, since=since, until=until,
                              account=args.account)
    if args.verb == "cleanup-obsolete":
        return emails.cleanup_obsolete(dry_run=args.dry_run,
                                       only_domain=args.only_domain,
                                       only_entity=args.only_entity,
                                       since=since, until=until)
    raise SystemExit(f"verbe inconnu : emails {args.verb}")


def _cmd_notes(args) -> Any:
    from connaissance.commands import notes
    since, until = _parse_date_range(args)
    if args.verb == "scan":
        return notes.scan(since=since, until=until, output_file=args.output_file)
    if args.verb == "backlog-count":
        return notes.backlog_count(since=since, until=until)
    if args.verb == "copy":
        return notes.copy(dry_run=args.dry_run, since=since, until=until)
    raise SystemExit(f"verbe inconnu : notes {args.verb}")


def _cmd_pipeline(args) -> Any:
    from connaissance.commands import pipeline
    since = getattr(args, "since", None)
    until = getattr(args, "until", None)
    if args.verb == "detect":
        steps = args.steps.split(",") if args.steps else ["all"]
        return pipeline.detect(steps=steps, source=args.source,
                               mode=args.mode, since=since, until=until)
    if args.verb == "costs":
        return pipeline.costs(mode=args.mode, since=since, until=until,
                              real=getattr(args, "real", False))
    raise SystemExit(f"verbe inconnu : pipeline {args.verb}")


def _cmd_organize(args) -> Any:
    from connaissance.commands import organize
    if args.verb == "plan":
        return organize.plan()
    if args.verb == "enrich":
        if getattr(args, "qmd_results_stdin", False):
            if sys.stdin.isatty():
                raise SystemExit(
                    "--qmd-results-stdin requiert un pipe : aucun contenu "
                    "n'a été envoyé sur stdin."
                )
            qmd_results = json.loads(sys.stdin.read() or "[]")
        else:
            qmd_results = json.loads(args.qmd_results) if args.qmd_results else []
        return organize.enrich(args.manifest, qmd_results)
    if args.verb == "apply":
        return organize.apply(args.manifest, dry_run=args.dry_run)
    if args.verb == "resolve":
        return organize.resolve(name=args.name, date=args.date,
                                title=args.title, alias=args.alias)
    raise SystemExit(f"verbe inconnu : organize {args.verb}")


def _cmd_optimize(args) -> Any:
    from connaissance.commands import optimize
    if args.verb == "plan":
        return optimize.plan()
    if args.verb == "apply":
        return optimize.apply(dry_run=args.dry_run)
    raise SystemExit(f"verbe inconnu : optimize {args.verb}")


def _cmd_summarize(args) -> Any:
    from connaissance.commands import summarize
    if args.verb == "plan":
        return summarize.plan(source=args.source)
    if args.verb == "prepare":
        paths_arg: list[str] | str
        # Accepter `--paths all` (sentinel littéral) comme équivalent de
        # « tous les chemins manquants » ; ne splitter que si on a une vraie
        # liste CSV.
        if not args.paths or args.paths == "all":
            paths_arg = "all"
        else:
            paths_arg = args.paths.split(",")
        return summarize.prepare(paths=paths_arg, mode=args.mode,
                                 source=args.source,
                                 output_file=args.output_file,
                                 preference=args.preference)
    if args.verb == "register":
        if args.from_results_file:
            return summarize.register_from_results_file(
                args.from_results_file,
                requests_file=args.requests_file,
                cleanup=not args.no_cleanup,
            )
        if args.stdin:
            if sys.stdin.isatty():
                raise SystemExit(
                    "--stdin requiert un pipe : aucun contenu n'a été envoyé sur stdin."
                )
            content = sys.stdin.read()
        else:
            content = args.content or ""
        return summarize.register(args.custom_id, content, source_path=args.source_path)
    raise SystemExit(f"verbe inconnu : summarize {args.verb}")


def _cmd_synthesis(args) -> Any:
    from connaissance.commands import synthesis
    if args.verb == "plan":
        return synthesis.plan()
    if args.verb == "aliases-candidates":
        return synthesis.aliases_candidates(args.entity)
    if args.verb == "relations-candidates":
        return synthesis.relations_candidates(args.entity)
    if args.verb == "entity-paths":
        return synthesis.entity_paths(args.entity)
    if args.verb == "list-all":
        return synthesis.list_all()
    if args.verb == "register":
        # Mode moderne : content + kind (+ entity). Le contenu peut arriver
        # via --content, --content-file, ou stdin (si --content-stdin).
        if args.from_results_file:
            return synthesis.register_from_results_file(
                args.from_results_file,
                requests_file=args.requests_file,
                cleanup=not args.no_cleanup,
            )
        content = None
        if getattr(args, "content_stdin", False):
            content = sys.stdin.read()
        elif getattr(args, "content_file", None):
            content = Path(args.content_file).read_text(encoding="utf-8")
        elif getattr(args, "content", None):
            content = args.content
        return synthesis.register(
            content=content,
            kind=getattr(args, "kind", None),
            entity=getattr(args, "entity", None),
            rel_path=getattr(args, "rel_path", None),
            source_type=getattr(args, "source_type", None),
            source_path=getattr(args, "source_path", None),
        )
    if args.verb == "prepare":
        ents: list[str] | str
        if not args.entities or args.entities == "stale":
            ents = "stale"
        else:
            ents = args.entities.split(",")
        return synthesis.prepare(entities=ents,
                                 preference=args.preference,
                                 output_file=args.output_file)
    raise SystemExit(f"verbe inconnu : synthesis {args.verb}")


def _cmd_audit(args) -> Any:
    from connaissance.commands import audit
    if args.verb == "check":
        steps = args.steps.split(",") if args.steps else ["all"]
        return audit.check(steps=steps)
    if args.verb == "reindex-db":
        return audit.reindex_db(dry_run=args.dry_run)
    if args.verb == "repair-attachments":
        return audit.repair_attachments(dry_run=args.dry_run)
    if args.verb == "archive-non-documents":
        return audit.archive_non_documents(dry_run=args.dry_run)
    raise SystemExit(f"verbe inconnu : audit {args.verb}")


def _cmd_actions(args) -> Any:
    from connaissance.commands import actions
    if args.verb == "list":
        return actions.list_actions(status=args.status, entity=args.entity)
    raise SystemExit(f"verbe inconnu : actions {args.verb}")


def _cmd_scope(args) -> Any:
    from connaissance.commands import scope
    if args.verb == "scan":
        return scope.scan(depth=args.depth)
    if args.verb == "check":
        return scope.check()
    if args.verb == "include":
        return scope.include(args.folder)
    if args.verb == "exclude":
        return scope.exclude(args.folder)
    raise SystemExit(f"verbe inconnu : scope {args.verb}")


def _cmd_config(args) -> Any:
    from connaissance.commands import config as config_cmd
    if args.verb == "scoring-show":
        return config_cmd.scoring_show()
    if args.verb == "scoring-set":
        atoms: dict[str, Any] = {}
        if args.add_domain_marketing:
            atoms["add_domain_marketing"] = args.add_domain_marketing.split(",")
        if args.remove_domain_marketing:
            atoms["remove_domain_marketing"] = args.remove_domain_marketing.split(",")
        if args.add_domain_personnel:
            atoms["add_domain_personnel"] = args.add_domain_personnel.split(",")
        if args.remove_domain_personnel:
            atoms["remove_domain_personnel"] = args.remove_domain_personnel.split(",")
        if args.add_pattern_actionnable:
            atoms["add_pattern_actionnable"] = [args.add_pattern_actionnable]
        if args.add_pattern_promotionnel:
            atoms["add_pattern_promotionnel"] = [args.add_pattern_promotionnel]
        if args.set_weight:
            atoms["set_weight"] = {k: int(v) for k, v in [p.split("=") for p in args.set_weight.split(",")]}
        if args.set_seuil:
            atoms["set_seuil"] = {k: int(v) for k, v in [p.split("=") for p in args.set_seuil.split(",")]}
        return config_cmd.scoring_set(dry_run=args.dry_run, **atoms)
    if args.verb == "scoring-diff":
        return config_cmd.scoring_diff()
    if args.verb == "scoring-validate":
        return config_cmd.scoring_validate()
    raise SystemExit(f"verbe inconnu : config {args.verb}")


def _cmd_manifest(args) -> Any:
    from connaissance.commands import manifest
    if args.verb == "patch":
        if getattr(args, "patches_stdin", False):
            if sys.stdin.isatty():
                raise SystemExit(
                    "--patches-stdin requiert un pipe : aucun contenu "
                    "n'a été envoyé sur stdin."
                )
            patches = json.loads(sys.stdin.read() or "[]")
        elif args.patches:
            patches = json.loads(args.patches)
        else:
            patches = None
        return manifest.patch(
            args.manifest,
            patches=patches,
            filter_expr=args.filter,
            set_expr=args.set,
            delete_filter=args.delete_filter,
        )
    raise SystemExit(f"verbe inconnu : manifest {args.verb}")


_GROUPS: dict[str, Callable] = {
    "documents": _cmd_documents,
    "emails": _cmd_emails,
    "notes": _cmd_notes,
    "pipeline": _cmd_pipeline,
    "organize": _cmd_organize,
    "optimize": _cmd_optimize,
    "summarize": _cmd_summarize,
    "synthesis": _cmd_synthesis,
    "audit": _cmd_audit,
    "actions": _cmd_actions,
    "scope": _cmd_scope,
    "config": _cmd_config,
    "manifest": _cmd_manifest,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="connaissance",
        description="CLI déterministe du plugin connaissance.",
    )
    parser.add_argument("--human", action="store_true",
                        help="Sortie humaine lisible (debug). Défaut : JSON.")

    sub = parser.add_subparsers(dest="group", required=True)

    def add_date_range(p):
        p.add_argument("--since", type=str, default=None)
        p.add_argument("--until", type=str, default=None)

    # documents
    p_doc = sub.add_parser("documents")
    p_doc_verbs = p_doc.add_subparsers(dest="verb", required=True)
    p_doc_scan = p_doc_verbs.add_parser("scan")
    add_date_range(p_doc_scan)
    p_doc_scan.add_argument("--output-file", dest="output_file", type=str,
                            default=None,
                            help="Écrire le scan complet dans ce fichier JSON "
                                 "au lieu de le renvoyer inline (peut dépasser "
                                 "le Mo sur une base documentaire chargée).")
    p_doc_bc = p_doc_verbs.add_parser("backlog-count")
    add_date_range(p_doc_bc)
    p_doc_reg = p_doc_verbs.add_parser("register")
    p_doc_reg.add_argument("source_file")
    p_doc_reg.add_argument("transcription")
    p_doc_verbs.add_parser("register-existing")
    p_doc_verbs.add_parser("suspects")
    p_doc_vp = p_doc_verbs.add_parser("verify-preserve")
    p_doc_vp.add_argument("before")
    p_doc_vp.add_argument("after")

    # emails
    p_em = sub.add_parser("emails")
    p_em_verbs = p_em.add_subparsers(dest="verb", required=True)
    for verb in ("stats", "backlog-count", "extract", "threads", "calibrate", "senders", "cleanup-obsolete"):
        vp = p_em_verbs.add_parser(verb)
        vp.add_argument("--account", type=str, default=None)
        vp.add_argument("--folder", type=str, default=None)
        add_date_range(vp)
        if verb in ("extract", "cleanup-obsolete"):
            vp.add_argument("--dry-run", action="store_true")
        if verb == "extract":
            vp.add_argument("--no-images", action="store_true")
        if verb in ("calibrate", "senders"):
            vp.add_argument("--sample", type=int, default=None)
        if verb == "cleanup-obsolete":
            vp.add_argument("--only-domain", type=str, default=None)
            vp.add_argument("--only-entity", type=str, default=None)

    # notes
    p_notes = sub.add_parser("notes")
    p_notes_verbs = p_notes.add_subparsers(dest="verb", required=True)
    p_notes_scan = p_notes_verbs.add_parser("scan")
    add_date_range(p_notes_scan)
    p_notes_scan.add_argument("--output-file", dest="output_file", type=str,
                              default=None,
                              help="Écrire le scan complet dans ce fichier JSON "
                                   "au lieu de le renvoyer inline (peut dépasser "
                                   "plusieurs centaines de Ko sur un Apple "
                                   "Notes chargé).")
    p_notes_bc = p_notes_verbs.add_parser("backlog-count")
    add_date_range(p_notes_bc)
    p_notes_copy = p_notes_verbs.add_parser("copy")
    p_notes_copy.add_argument("--dry-run", action="store_true")
    add_date_range(p_notes_copy)

    # pipeline
    p_pipe = sub.add_parser("pipeline")
    p_pipe_verbs = p_pipe.add_subparsers(dest="verb", required=True)
    p_pipe_detect = p_pipe_verbs.add_parser("detect")
    p_pipe_detect.add_argument("--steps", type=str, default=None)
    p_pipe_detect.add_argument("--source", type=str, default=None)
    p_pipe_detect.add_argument("--mode", type=str, default="batch")
    add_date_range(p_pipe_detect)
    p_pipe_costs = p_pipe_verbs.add_parser("costs")
    p_pipe_costs.add_argument("--mode", type=str, default="batch")
    p_pipe_costs.add_argument("--real", action="store_true",
                              help="Coûts réels mesurés (depuis le journal "
                                   "llm_usage) au lieu de l'estimation "
                                   "forfaitaire.")
    add_date_range(p_pipe_costs)
    # organize
    p_org = sub.add_parser("organize")
    p_org_verbs = p_org.add_subparsers(dest="verb", required=True)
    p_org_verbs.add_parser("plan")
    p_org_enr = p_org_verbs.add_parser("enrich")
    p_org_enr.add_argument("manifest")
    p_org_enr.add_argument("--qmd-results", type=str, default=None,
                           help="JSON array inline. Volumineux : préférer "
                                "--qmd-results-stdin pour éviter le ps -ef leak.")
    p_org_enr.add_argument("--qmd-results-stdin", dest="qmd_results_stdin",
                           action="store_true",
                           help="Lire le JSON des résultats qmd depuis stdin.")
    p_org_apply = p_org_verbs.add_parser("apply")
    p_org_apply.add_argument("manifest")
    p_org_apply.add_argument("--dry-run", action="store_true")
    p_org_res = p_org_verbs.add_parser("resolve")
    p_org_res.add_argument("--name", type=str, default=None)
    p_org_res.add_argument("--date", type=str, default=None)
    p_org_res.add_argument("--title", type=str, default=None)
    p_org_res.add_argument("--alias", type=str, default=None)

    # optimize
    p_opt = sub.add_parser("optimize")
    p_opt_verbs = p_opt.add_subparsers(dest="verb", required=True)
    p_opt_verbs.add_parser("plan")
    p_opt_apply = p_opt_verbs.add_parser("apply")
    p_opt_apply.add_argument("--dry-run", action="store_true")

    # summarize
    p_sum = sub.add_parser("summarize")
    p_sum_verbs = p_sum.add_subparsers(dest="verb", required=True)
    p_sum_plan = p_sum_verbs.add_parser("plan")
    p_sum_plan.add_argument("--source", type=str, default=None)
    p_sum_prep = p_sum_verbs.add_parser("prepare")
    p_sum_prep.add_argument("--paths", type=str, default=None)
    p_sum_prep.add_argument("--mode", type=str, default="batch")
    p_sum_prep.add_argument("--source", type=str, default=None)
    p_sum_prep.add_argument("--preference",
                            choices=["auto", "quality", "economy"],
                            default="auto",
                            help="Pilote l'heuristique de choix de modèle. "
                                 "'auto' (défaut) mélange Sonnet et Haiku ; "
                                 "'economy' force Haiku sauf documents longs "
                                 "et fils ; 'quality' force Sonnet.")
    p_sum_prep.add_argument("--output-file", dest="output_file", type=str,
                            default=None,
                            help="Écrire les requests dans ce fichier JSON au lieu "
                                 "de les renvoyer inline (évite de polluer le contexte "
                                 "de l'assistant).")
    p_sum_reg = p_sum_verbs.add_parser("register")
    # custom_id est optionnel : requis pour register single, inutile pour
    # register batch depuis --from-results-file.
    p_sum_reg.add_argument("custom_id", nargs="?", default=None)
    p_sum_reg.add_argument("--content", type=str, default=None)
    p_sum_reg.add_argument("--source-path", dest="source_path", type=str, default=None)
    p_sum_reg.add_argument("--stdin", action="store_true")
    p_sum_reg.add_argument("--from-results-file", dest="from_results_file",
                           type=str, default=None,
                           help="Enregistrer en masse depuis un fichier de résultats "
                                "API (sortie de claude_api__wait_for_batch ou "
                                "query_direct avec output_file). Itère sur chaque "
                                "item sans charger les contents dans le contexte "
                                "de l'appelant.")
    p_sum_reg.add_argument("--requests-file", dest="requests_file",
                           type=str, default=None,
                           help="Fichier de prep (sortie de summarize_prepare "
                                "--output-file). Utilisé avec --from-results-file "
                                "pour remplir le fallback source_path par "
                                "custom_id quand le LLM a oublié d'injecter "
                                "`source:` dans le frontmatter.")
    p_sum_reg.add_argument("--no-cleanup", dest="no_cleanup",
                           action="store_true",
                           help="Conserver les fichiers de transit "
                                "(results_file, requests_file) après "
                                "l'enregistrement. Par défaut ils sont "
                                "supprimés si aucune erreur.")

    # synthesis
    p_syn = sub.add_parser("synthesis")
    p_syn_verbs = p_syn.add_subparsers(dest="verb", required=True)
    p_syn_verbs.add_parser("plan")
    p_syn_ac = p_syn_verbs.add_parser("aliases-candidates")
    p_syn_ac.add_argument("--entity", type=str, required=True)
    p_syn_rc = p_syn_verbs.add_parser("relations-candidates")
    p_syn_rc.add_argument("--entity", type=str, required=True)
    p_syn_ep = p_syn_verbs.add_parser("entity-paths")
    p_syn_ep.add_argument("--entity", type=str, required=True)
    p_syn_verbs.add_parser("list-all")
    p_syn_reg = p_syn_verbs.add_parser("register")
    # Mode moderne : --kind + (optionnel) --entity + contenu
    p_syn_reg.add_argument("--kind", dest="kind",
                           choices=["fiche", "chronologie", "moc", "digest", "index"],
                           default=None,
                           help="Type de synthèse — détermine le chemin de destination.")
    p_syn_reg.add_argument("--entity", dest="entity", default=None,
                           help="fiche/chronologie : 'type/slug' ; moc : slug "
                                "de catégorie ; digest : date YYYY-MM-DD (défaut : "
                                "aujourd'hui) ; index : ignoré.")
    p_syn_reg.add_argument("--content", dest="content", default=None,
                           help="Markdown à écrire (alternatives : --content-file, --content-stdin).")
    p_syn_reg.add_argument("--content-file", dest="content_file", default=None,
                           help="Fichier dont le contenu sera écrit (évite les soucis d'échappement shell).")
    p_syn_reg.add_argument("--content-stdin", dest="content_stdin", action="store_true",
                           help="Lire le contenu depuis stdin.")
    # Mode hérité : --rel-path + --source-type + --source-path (DB uniquement)
    p_syn_reg.add_argument("--rel-path", dest="rel_path", default=None,
                           help="[mode hérité] Chemin relatif à ~/Connaissance/ — "
                                "enregistre seulement dans la DB, n'écrit pas de fichier.")
    p_syn_reg.add_argument("--source-type", dest="source_type", default=None)
    p_syn_reg.add_argument("--source-path", dest="source_path", default=None)
    # Mode batch API : enregistrer fiches+chronologies depuis un fichier de résultats
    p_syn_reg.add_argument("--from-results-file", dest="from_results_file",
                           type=str, default=None,
                           help="Enregistre en masse les paires fiche+chronologie "
                                "depuis un fichier de résultats API (sortie de "
                                "claude_api__wait_for_batch ou query_direct). "
                                "Split chaque content sur les marqueurs "
                                "<!-- FICHE --> / <!-- CHRONOLOGIE --> et double-"
                                "register sans charger le contenu dans le contexte.")
    p_syn_reg.add_argument("--requests-file", dest="requests_file",
                           type=str, default=None,
                           help="Fichier de prep (sortie de synthesis prepare "
                                "--output-file). Requis avec --from-results-file : "
                                "fournit le mapping custom_id → entity.")
    p_syn_reg.add_argument("--no-cleanup", dest="no_cleanup", action="store_true")
    # synthesis prepare — construit les requests fiche+chronologie pour l'API
    p_syn_prep = p_syn_verbs.add_parser("prepare")
    p_syn_prep.add_argument("--entities", type=str, default=None,
                            help="Liste CSV 'type/slug,type/slug,…' ou omettre "
                                 "pour cibler toutes les entités 'stale'.")
    p_syn_prep.add_argument("--preference",
                            choices=["auto", "quality", "economy"],
                            default="auto")
    p_syn_prep.add_argument("--output-file", dest="output_file", type=str,
                            default=None,
                            help="Chemin JSON où écrire les requests "
                                 "(évite de polluer le contexte de l'assistant).")

    # audit
    p_aud = sub.add_parser("audit")
    p_aud_verbs = p_aud.add_subparsers(dest="verb", required=True)
    p_aud_check = p_aud_verbs.add_parser("check")
    p_aud_check.add_argument("--steps", type=str, default=None)
    for verb in ("reindex-db", "repair-attachments", "archive-non-documents"):
        vp = p_aud_verbs.add_parser(verb)
        vp.add_argument("--dry-run", action="store_true")

    # actions
    p_act = sub.add_parser("actions")
    p_act_verbs = p_act.add_subparsers(dest="verb", required=True)
    p_act_list = p_act_verbs.add_parser("list")
    p_act_list.add_argument("--status", type=str, default="all",
                            choices=["all", "ouverte", "expiree"])
    p_act_list.add_argument("--entity", type=str, default=None)

    # scope
    p_sc = sub.add_parser("scope")
    p_sc_verbs = p_sc.add_subparsers(dest="verb", required=True)
    p_sc_scan = p_sc_verbs.add_parser("scan")
    p_sc_scan.add_argument("--depth", type=int, default=3)
    p_sc_verbs.add_parser("check")
    p_sc_inc = p_sc_verbs.add_parser("include")
    p_sc_inc.add_argument("folder")
    p_sc_exc = p_sc_verbs.add_parser("exclude")
    p_sc_exc.add_argument("folder")

    # config
    p_cfg = sub.add_parser("config")
    p_cfg_verbs = p_cfg.add_subparsers(dest="verb", required=True)
    p_cfg_verbs.add_parser("scoring-show")
    p_cfg_set = p_cfg_verbs.add_parser("scoring-set")
    p_cfg_set.add_argument("--add-domain-marketing", type=str, default=None,
                           dest="add_domain_marketing")
    p_cfg_set.add_argument("--remove-domain-marketing", type=str, default=None,
                           dest="remove_domain_marketing")
    p_cfg_set.add_argument("--add-domain-personnel", type=str, default=None,
                           dest="add_domain_personnel")
    p_cfg_set.add_argument("--remove-domain-personnel", type=str, default=None,
                           dest="remove_domain_personnel")
    p_cfg_set.add_argument("--add-pattern-actionnable", type=str, default=None,
                           dest="add_pattern_actionnable")
    p_cfg_set.add_argument("--add-pattern-promotionnel", type=str, default=None,
                           dest="add_pattern_promotionnel")
    p_cfg_set.add_argument("--set-weight", type=str, default=None, dest="set_weight",
                           help="k1=v1,k2=v2")
    p_cfg_set.add_argument("--set-seuil", type=str, default=None, dest="set_seuil",
                           help="capturer=0,ignorer=-1")
    p_cfg_set.add_argument("--dry-run", action="store_true", default=True)
    p_cfg_set.add_argument("--apply", dest="dry_run", action="store_false")
    p_cfg_verbs.add_parser("scoring-diff")
    p_cfg_verbs.add_parser("scoring-validate")

    # manifest
    p_mf = sub.add_parser("manifest")
    p_mf_verbs = p_mf.add_subparsers(dest="verb", required=True)
    p_mf_patch = p_mf_verbs.add_parser("patch")
    p_mf_patch.add_argument("manifest")
    p_mf_patch.add_argument("--patches", type=str, default=None,
                            help="JSON array de patches ciblés. Pour les "
                                 "lots volumineux, préférer --patches-stdin.")
    p_mf_patch.add_argument("--patches-stdin", dest="patches_stdin",
                            action="store_true",
                            help="Lire le JSON array de patches depuis stdin.")
    p_mf_patch.add_argument("--filter", type=str, default=None,
                            help="k1=v1,k2=v2 pour patch en masse")
    p_mf_patch.add_argument("--set", type=str, default=None,
                            help="k1=v1,k2=v2 à appliquer aux entrées matchées")
    p_mf_patch.add_argument("--delete-filter", dest="delete_filter", type=str, default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    handler = _GROUPS.get(args.group)
    if handler is None:
        parser.error(f"groupe inconnu : {args.group}")

    try:
        result = handler(args)
    except Exception as exc:
        err = {"error": {"type": type(exc).__name__, "message": str(exc)}}
        print(json.dumps(err, indent=2, ensure_ascii=False), file=sys.stderr)
        return 1

    _json_print(result, human=getattr(args, "human", False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
