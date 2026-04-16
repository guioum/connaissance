// MCP server wrapper for the `connaissance` CLI.
//
// Exposes 42 tools (mcp__connaissance__*) that shell-out to the
// `connaissance` Python CLI installed via `uv tool install` or `pip`.
// Each tool maps 1:1 to a CLI subcommand `connaissance <group> <verb>`.
//
// Pattern lifted from guioum/mistral-ocr — zero business logic in Node.

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { homedir } from "node:os";
import { join } from "node:path";
import { existsSync } from "node:fs";

const execFileAsync = promisify(execFile);

function findCli() {
  const envVal = process.env.CONNAISSANCE_CLI;
  // Accept the env var only if it's a real path — ignore empty strings and
  // unresolved ${user_config.xxx} placeholders (which some MCP hosts pass
  // literally when the user leaves an optional config field empty).
  if (envVal && envVal.trim() && !envVal.includes("${")) return envVal;
  const localBin = join(homedir(), ".local", "bin", "connaissance");
  if (existsSync(localBin)) return localBin;
  return "connaissance";
}

const CLI = findCli();

async function runCli(group, verb, args = []) {
  const fullArgs = [group, verb, ...args];
  try {
    const { stdout, stderr } = await execFileAsync(CLI, fullArgs, {
      env: { ...process.env },
      maxBuffer: 100 * 1024 * 1024, // 100 MB for large payloads (prompts, large extracts)
      timeout: 600_000, // 10 minutes (emails extract can be long)
    });
    if (stderr && stderr.trim()) {
      try {
        const parsed = JSON.parse(stderr.trim());
        throw new Error(parsed?.error?.message || parsed?.error || stderr.trim());
      } catch (e) {
        if (e instanceof SyntaxError) throw new Error(stderr.trim());
        throw e;
      }
    }
    if (!stdout || !stdout.trim()) return {};
    return JSON.parse(stdout);
  } catch (err) {
    // Wrap ENOENT with a clearer message — the CLI must be installed globally
    if (err.code === "ENOENT") {
      throw new Error(
        `connaissance CLI not found at "${CLI}". ` +
        `Install with: uv tool install git+https://github.com/guioum/connaissance`
      );
    }
    throw err;
  }
}

function asToolResult(data) {
  return {
    content: [{
      type: "text",
      text: typeof data === "string" ? data : JSON.stringify(data, null, 2),
    }],
  };
}

function errorResult(message) {
  return { content: [{ type: "text", text: JSON.stringify({ error: message }, null, 2) }], isError: true };
}

function safeError(err) {
  return err instanceof Error ? err.message : String(err);
}

// Helper : construit une liste d'args CLI à partir d'un dict input, en
// poussant `--flag value` si la valeur est truthy non-null et non-undefined.
function pushFlag(args, name, value) {
  if (value === undefined || value === null) return;
  if (typeof value === "boolean") {
    if (value) args.push(`--${name}`);
    return;
  }
  args.push(`--${name}`, String(value));
}

const server = new McpServer({
  name: "connaissance",
  version: "2.1.0",
});

// ── Common schema snippets ─────────────────────────────────────

const dateRangeSchema = {
  since: z.string().optional().describe("Date ISO YYYY-MM-DD (inclusive)."),
  until: z.string().optional().describe("Date ISO YYYY-MM-DD (exclusive)."),
};

const emailsCommonSchema = {
  account: z.string().optional().describe("Path to a specific mbox account directory."),
  folder: z.string().optional().describe("Comma-separated mbox folder name(s)."),
  ...dateRangeSchema,
};

function emailsCommonArgs(args) {
  const out = [];
  pushFlag(out, "account", args.account);
  pushFlag(out, "folder", args.folder);
  pushFlag(out, "since", args.since);
  pushFlag(out, "until", args.until);
  return out;
}

// Generic tool wrapper : runs the CLI command and returns the JSON result
// as a text tool result. Errors are caught and returned as errorResult.
async function runAndFormat(group, verb, args) {
  try {
    const data = await runCli(group, verb, args);
    return asToolResult(data);
  } catch (err) {
    return errorResult(`${group} ${verb} failed: ${safeError(err)}`);
  }
}

// ── pipeline ───────────────────────────────────────────────────

server.registerTool(
  "connaissance_pipeline_detect",
  {
    description: "Detect outstanding pipeline work : missing summaries, unorganized summaries, stale syntheses, stale MOCs, cost estimates, DB stats.",
    inputSchema: {
      steps: z.string().optional().describe("Comma-separated subset of: resumes_manquants, resumes_perimes, non_organises, synthese_perimee, moc_perimes, couts, stats. Default 'all'."),
      source: z.enum(["document", "courriel", "note"]).optional().describe("Filter by source type."),
      mode: z.enum(["batch", "interactif"]).default("batch").describe("Cost estimation mode."),
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => {
    const a = [];
    pushFlag(a, "steps", args.steps);
    pushFlag(a, "source", args.source);
    pushFlag(a, "mode", args.mode);
    return runAndFormat("pipeline", "detect", a);
  }
);

server.registerTool(
  "connaissance_pipeline_costs",
  {
    description: "Estimate pipeline cost in USD for the current backlog (missing summaries, stale entities, stale MOCs).",
    inputSchema: {
      mode: z.enum(["batch", "interactif"]).default("batch").describe("Batch API gets 50% discount vs interactive."),
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => runAndFormat("pipeline", "costs", ["--mode", args.mode ?? "batch"])
);

server.registerTool(
  "connaissance_pipeline_simulate",
  {
    description: "Composite dry-run : detect + costs + documents.scan. Use at the start of a pipeline run to preview everything.",
    inputSchema: {
      mode: z.enum(["batch", "interactif"]).default("batch"),
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => runAndFormat("pipeline", "simulate", ["--mode", args.mode ?? "batch"])
);

// ── documents ──────────────────────────────────────────────────

server.registerTool(
  "connaissance_documents_scan",
  {
    description: "Scan ~/Documents/ and list files to transcribe. Applies filtres.yaml (extensions, excluded folders, date range).",
    inputSchema: dateRangeSchema,
    annotations: { readOnlyHint: true },
  },
  async (args) => {
    const a = [];
    pushFlag(a, "since", args.since);
    pushFlag(a, "until", args.until);
    return runAndFormat("documents", "scan", a);
  }
);

server.registerTool(
  "connaissance_documents_register",
  {
    description: "Register a document transcription in tracking.db and inject canonical frontmatter (source, source_hash, transcribed_at).",
    inputSchema: {
      source_file: z.string().describe("Absolute path to the original document file (PDF, image, etc.)."),
      transcription: z.string().describe("Absolute path to the generated transcription markdown."),
    },
  },
  async (args) => runAndFormat("documents", "register", [args.source_file, args.transcription])
);

server.registerTool(
  "connaissance_documents_register_existing",
  {
    description: "Recovery tool : scan all existing transcriptions and register them in tracking.db. Idempotent.",
    inputSchema: {},
  },
  async () => runAndFormat("documents", "register-existing", [])
);

server.registerTool(
  "connaissance_documents_suspects",
  {
    description: "List transcriptions with suspect table patterns (empty cells, orphan pipe lines) that might need re-formatting via the transcrire/fix-ocr skill.",
    inputSchema: {},
    annotations: { readOnlyHint: true },
  },
  async () => runAndFormat("documents", "suspects", [])
);

server.registerTool(
  "connaissance_documents_verify_preserve",
  {
    description: "Verify that a corrected transcription preserves the textual content of the original (tokenization comparison). Used by fix-ocr to ensure strict preservation.",
    inputSchema: {
      before: z.string().describe("Path (or raw content) of the original markdown."),
      after: z.string().describe("Path (or raw content) of the corrected markdown."),
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => runAndFormat("documents", "verify-preserve", [args.before, args.after])
);

// ── emails ─────────────────────────────────────────────────────

server.registerTool(
  "connaissance_emails_stats",
  {
    description: "Count emails per mbox folder without extracting. Useful to estimate workload.",
    inputSchema: emailsCommonSchema,
    annotations: { readOnlyHint: true },
  },
  async (args) => runAndFormat("emails", "stats", emailsCommonArgs(args))
);

server.registerTool(
  "connaissance_emails_extract",
  {
    description: "Extract emails from mbox archives to markdown transcriptions. Applies multi-signal scoring filter. Writes to Transcriptions/Courriels/ and updates tracking.db.",
    inputSchema: {
      ...emailsCommonSchema,
      dry_run: z.boolean().default(false).describe("Preview without writing."),
      no_images: z.boolean().default(false).describe("Only extract PDFs as attachments, skip images."),
    },
  },
  async (args) => {
    const a = emailsCommonArgs(args);
    if (args.dry_run) a.push("--dry-run");
    if (args.no_images) a.push("--no-images");
    return runAndFormat("emails", "extract", a);
  }
);

server.registerTool(
  "connaissance_emails_threads",
  {
    description: "Group emails into threads via In-Reply-To / References headers (union-find). Returns {threads, orphans, filtered_below_score}.",
    inputSchema: emailsCommonSchema,
    annotations: { readOnlyHint: true },
  },
  async (args) => runAndFormat("emails", "threads", emailsCommonArgs(args))
);

server.registerTool(
  "connaissance_emails_calibrate",
  {
    description: "Score a sample of emails and produce proposed_mutations to tune scoring-courriels.yaml. Returns atoms ready for config scoring-set.",
    inputSchema: {
      ...emailsCommonSchema,
      sample: z.number().int().positive().default(200).describe("Sample size (default 200)."),
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => {
    const a = emailsCommonArgs(args);
    pushFlag(a, "sample", args.sample);
    return runAndFormat("emails", "calibrate", a);
  }
);

server.registerTool(
  "connaissance_emails_senders",
  {
    description: "Analyze borderline senders (whitelist/blacklist candidates) over a sample.",
    inputSchema: {
      ...emailsCommonSchema,
      sample: z.number().int().positive().default(500).describe("Sample size (default 500)."),
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => {
    const a = emailsCommonArgs(args);
    pushFlag(a, "sample", args.sample);
    return runAndFormat("emails", "senders", a);
  }
);

server.registerTool(
  "connaissance_emails_cleanup_obsolete",
  {
    description: "Re-score existing email transcriptions against current scoring rules and archive those below threshold. Reversible (moves to .archive/courriels-depublies/).",
    inputSchema: {
      ...emailsCommonSchema,
      dry_run: z.boolean().default(true).describe("Default dry-run ; pass false to actually archive."),
      only_domain: z.string().optional().describe("Comma-separated domains to limit scope."),
      only_entity: z.string().optional().describe("Entity identifier in type/slug format (e.g., 'personnes/marie-dubois')."),
    },
  },
  async (args) => {
    const a = emailsCommonArgs(args);
    if (args.dry_run !== false) a.push("--dry-run");
    pushFlag(a, "only-domain", args.only_domain);
    pushFlag(a, "only-entity", args.only_entity);
    return runAndFormat("emails", "cleanup-obsolete", a);
  }
);

// ── notes ──────────────────────────────────────────────────────

server.registerTool(
  "connaissance_notes_scan",
  {
    description: "Scan ~/Notes/ and list Apple Notes markdown files to copy into the knowledge base.",
    inputSchema: dateRangeSchema,
    annotations: { readOnlyHint: true },
  },
  async (args) => {
    const a = [];
    pushFlag(a, "since", args.since);
    pushFlag(a, "until", args.until);
    return runAndFormat("notes", "scan", a);
  }
);

server.registerTool(
  "connaissance_notes_copy",
  {
    description: "Copy Apple Notes to Transcriptions/Notes/ incrementally. Preserves referenced attachments and frontmatter dates.",
    inputSchema: {
      ...dateRangeSchema,
      dry_run: z.boolean().default(false).describe("Preview without writing."),
    },
  },
  async (args) => {
    const a = [];
    if (args.dry_run) a.push("--dry-run");
    pushFlag(a, "since", args.since);
    pushFlag(a, "until", args.until);
    return runAndFormat("notes", "copy", a);
  }
);

// ── organize ───────────────────────────────────────────────────

server.registerTool(
  "connaissance_organize_plan",
  {
    description: "Build an organization manifest for unorganized summaries. Each row is tagged auto / alias_match / a_confirmer. Writes manifest to disk. Does NOT move files.",
    inputSchema: {},
    annotations: { readOnlyHint: true },
  },
  async () => runAndFormat("organize", "plan", [])
);

server.registerTool(
  "connaissance_organize_enrich",
  {
    description: "Enrich an existing manifest with qmd candidates for a_confirmer rows. The caller pre-queries qmd and passes the candidates back here for injection.",
    inputSchema: {
      manifest: z.string().describe("Absolute path to the manifest JSON."),
      qmd_results: z.array(z.object({
        id: z.string(),
        candidates: z.array(z.any()),
      })).describe("Array of {id, candidates} — id matches entry.id or entry.resume_path."),
    },
  },
  async (args) => runAndFormat("organize", "enrich", [args.manifest, "--qmd-results", JSON.stringify(args.qmd_results)])
);

server.registerTool(
  "connaissance_organize_apply",
  {
    description: "Apply an organization manifest : move summaries, transcriptions, original documents to their entity directories. Always dry-run first.",
    inputSchema: {
      manifest: z.string().describe("Absolute path to the manifest JSON."),
      dry_run: z.boolean().default(false).describe("Preview file moves without executing."),
    },
  },
  async (args) => {
    const a = [args.manifest];
    if (args.dry_run) a.push("--dry-run");
    return runAndFormat("organize", "apply", a);
  }
);

server.registerTool(
  "connaissance_organize_resolve",
  {
    description: "Deterministic helpers : compute slug from a name, build a filename from date+title, look up an alias in existing fiches.",
    inputSchema: {
      name: z.string().optional().describe("Entity name to slugify."),
      date: z.string().optional().describe("Date for filename (YYYY-MM-DD)."),
      title: z.string().optional().describe("Title for filename."),
      alias: z.string().optional().describe("Identifier (name/email/domain) to look up."),
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => {
    const a = [];
    pushFlag(a, "name", args.name);
    pushFlag(a, "date", args.date);
    pushFlag(a, "title", args.title);
    pushFlag(a, "alias", args.alias);
    return runAndFormat("organize", "resolve", a);
  }
);

// ── optimize ───────────────────────────────────────────────────

server.registerTool(
  "connaissance_optimize_plan",
  {
    description: "List document attachments to promote to ~/Documents/promus/ and duplicate files by SHA256.",
    inputSchema: {},
    annotations: { readOnlyHint: true },
  },
  async () => runAndFormat("optimize", "plan", [])
);

server.registerTool(
  "connaissance_optimize_apply",
  {
    description: "Promote document attachments and deduplicate identical files. Dry-run by default.",
    inputSchema: {
      dry_run: z.boolean().default(false),
    },
  },
  async (args) => {
    const a = [];
    if (args.dry_run) a.push("--dry-run");
    return runAndFormat("optimize", "apply", a);
  }
);

// ── summarize ──────────────────────────────────────────────────

server.registerTool(
  "connaissance_summarize_plan",
  {
    description: "List transcriptions with missing summaries. Returns {missing: [{id, path, file_type}]} ready for summarize_prepare.",
    inputSchema: {
      source: z.enum(["document", "courriel", "note"]).optional(),
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => {
    const a = [];
    pushFlag(a, "source", args.source);
    return runAndFormat("summarize", "plan", a);
  }
);

server.registerTool(
  "connaissance_summarize_prepare",
  {
    description:
      "Build LLM requests from prompt templates + transcription content. " +
      "Returns {requests: [{custom_id, system, user, model, max_tokens}]} ready " +
      "for any generation mode (API batch, API direct, or subagent). " +
      "'paths' must be FILE PATHS of transcriptions (the 'path' field from " +
      "summarize_plan), not custom_ids or hashes. " +
      "'mode' controls the request FORMAT only — always use 'direct', even if " +
      "you plan to process the requests via subagents.",
    inputSchema: {
      paths: z.union([z.string(), z.array(z.string())]).optional().describe(
        "Transcription file paths (e.g., 'Transcriptions/Documents/org/file.md'). " +
        "Pass a comma-separated string or an array of strings. Omit for 'all'. " +
        "Use the 'path' values from summarize_plan — NOT custom_ids."
      ),
      mode: z.enum(["batch", "direct", "inline"]).default("direct").describe(
        "Request format. Use 'direct' in all cases (including subagent processing). " +
        "'inline' is accepted as an alias for 'direct'. 'batch' adds cache_control headers."
      ),
      source: z.enum(["document", "courriel", "note", "fil"]).optional().describe("Override source_type for template selection."),
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => {
    const a = [];
    let pathsVal = args.paths;
    if (Array.isArray(pathsVal)) pathsVal = pathsVal.join(",");
    pushFlag(a, "paths", pathsVal);
    // Normalize "inline" → "direct" (inline is a generation strategy, not a request format)
    const mode = args.mode === "inline" ? "direct" : (args.mode ?? "direct");
    pushFlag(a, "mode", mode);
    pushFlag(a, "source", args.source);
    return runAndFormat("summarize", "prepare", a);
  }
);

server.registerTool(
  "connaissance_summarize_register",
  {
    description: "Post-process a summary content returned by claude-api-mcp : parse the frontmatter, derive the destination path, write to Résumés/, update tracking.db.",
    inputSchema: {
      custom_id: z.string().describe("Custom ID from the summarize_prepare request."),
      content: z.string().describe("Full markdown content returned by the Anthropic API (with frontmatter)."),
      source_path: z.string().optional().describe("Fallback source path if content frontmatter is missing."),
    },
  },
  async (args) => {
    const a = [args.custom_id, "--content", args.content];
    pushFlag(a, "source-path", args.source_path);
    return runAndFormat("summarize", "register", a);
  }
);

// ── synthesis ──────────────────────────────────────────────────

server.registerTool(
  "connaissance_synthesis_plan",
  {
    description: "List entities and MOCs with stale syntheses (missing or out-of-date vs their source summaries).",
    inputSchema: {},
    annotations: { readOnlyHint: true },
  },
  async () => runAndFormat("synthesis", "plan", [])
);

server.registerTool(
  "connaissance_synthesis_aliases_candidates",
  {
    description: "Scan all summaries of an entity and extract alias candidates (entity_name, from, domain patterns) with support counts. support >= 2 can be auto-accepted.",
    inputSchema: {
      entity: z.string().describe("Entity identifier in 'type/slug' format (e.g., 'organismes/arc')."),
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => runAndFormat("synthesis", "aliases-candidates", ["--entity", args.entity])
);

server.registerTool(
  "connaissance_synthesis_relations_candidates",
  {
    description: "Extract relation candidates via co-mentions : scan all summaries of an entity for relations[] in frontmatter, count co-mentions of other entities.",
    inputSchema: {
      entity: z.string().describe("Entity identifier in 'type/slug' format."),
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => runAndFormat("synthesis", "relations-candidates", ["--entity", args.entity])
);

server.registerTool(
  "connaissance_synthesis_register",
  {
    description: "Register a fiche / chronologie / MOC written by Claude in tracking.db.",
    inputSchema: {
      rel_path: z.string().describe("Path relative to ~/Connaissance/ (e.g., 'Synthèse/organismes/arc/fiche.md')."),
      source_type: z.enum(["document", "courriel", "note"]).describe("Source type category."),
      source_path: z.string().describe("Path of the resume or transcription that triggered this synthesis."),
    },
  },
  async (args) => runAndFormat(
    "synthesis", "register",
    [args.rel_path, "--source-type", args.source_type, "--source-path", args.source_path]
  )
);

// ── audit ──────────────────────────────────────────────────────

server.registerTool(
  "connaissance_audit_check",
  {
    description: "Run deterministic integrity checks : broken links, invalid frontmatter, desynchronized triplets, missing attachments, duplicates, overdue actions.",
    inputSchema: {
      steps: z.string().optional().describe("Comma-separated subset of: liens_casses, frontmatter_invalide, triplets_desynchronises, attachements_manquants, doublons, actions_a_reviser. Default 'all'."),
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => {
    const a = [];
    pushFlag(a, "steps", args.steps);
    return runAndFormat("audit", "check", a);
  }
);

server.registerTool(
  "connaissance_audit_reindex_db",
  {
    description: "Rebuild tracking.db from the files on disk (recovery after DB reset or corruption). Preserves existing data — idempotent.",
    inputSchema: {
      dry_run: z.boolean().default(false),
    },
  },
  async (args) => {
    const a = [];
    if (args.dry_run) a.push("--dry-run");
    return runAndFormat("audit", "reindex-db", a);
  }
);

server.registerTool(
  "connaissance_audit_repair_attachments",
  {
    description: "Repair broken attachment references in document transcriptions. Copies files from a central Attachments/ directory into the per-document locations.",
    inputSchema: {
      dry_run: z.boolean().default(false),
    },
  },
  async (args) => {
    const a = [];
    if (args.dry_run) a.push("--dry-run");
    return runAndFormat("audit", "repair-attachments", a);
  }
);

server.registerTool(
  "connaissance_audit_archive_non_documents",
  {
    description: "Archive non-document folders (code, photos, bundles) out of ~/Documents/ into ~/Documents/- Archives/. Updates filtres.yaml to remove the moved paths.",
    inputSchema: {
      dry_run: z.boolean().default(true),
    },
  },
  async (args) => {
    const a = [];
    if (args.dry_run !== false) a.push("--dry-run");
    return runAndFormat("audit", "archive-non-documents", a);
  }
);

// ── scope ──────────────────────────────────────────────────────

server.registerTool(
  "connaissance_scope_scan",
  {
    description: "Scan ~/Documents/ tree and classify folders (documents, code_repo, photos_perso, bundle_app, ...). Writes a report to ~/Connaissance/.config/perimetre-rapport.json.",
    inputSchema: {
      depth: z.number().int().min(1).default(3).describe("Max scan depth."),
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => {
    const a = [];
    pushFlag(a, "depth", args.depth);
    return runAndFormat("scope", "scan", a);
  }
);

server.registerTool(
  "connaissance_scope_check",
  {
    description: "Check the current scope config in filtres.yaml. Returns counts of included / excluded paths and patterns.",
    inputSchema: {},
    annotations: { readOnlyHint: true },
  },
  async () => runAndFormat("scope", "check", [])
);

server.registerTool(
  "connaissance_scope_include",
  {
    description: "Add a folder path to filtres.yaml dossiers_inclus.",
    inputSchema: {
      folder: z.string().describe("Folder path relative to ~/Documents/."),
    },
  },
  async (args) => runAndFormat("scope", "include", [args.folder])
);

server.registerTool(
  "connaissance_scope_exclude",
  {
    description: "Add a folder path to filtres.yaml dossiers_exclus.",
    inputSchema: {
      folder: z.string().describe("Folder path relative to ~/Documents/."),
    },
  },
  async (args) => runAndFormat("scope", "exclude", [args.folder])
);

// ── config (scoring mutations via typed atoms) ─────────────────

server.registerTool(
  "connaissance_config_scoring_show",
  {
    description: "Return the current scoring-courriels.yaml config as a dict.",
    inputSchema: {},
    annotations: { readOnlyHint: true },
  },
  async () => runAndFormat("config", "scoring-show", [])
);

server.registerTool(
  "connaissance_config_scoring_set",
  {
    description: "Apply typed atomic mutations to scoring-courriels.yaml (ruamel.yaml preserves user comments). Dry-run by default.",
    inputSchema: {
      add_domain_marketing: z.string().optional().describe("Comma-separated list."),
      remove_domain_marketing: z.string().optional(),
      add_domain_personnel: z.string().optional(),
      add_pattern_actionnable: z.string().optional().describe("Regex pattern."),
      add_pattern_promotionnel: z.string().optional().describe("Regex pattern."),
      set_weight: z.string().optional().describe("key1=val1,key2=val2"),
      set_seuil: z.string().optional().describe("capturer=0,ignorer=-1"),
      dry_run: z.boolean().default(true).describe("Pass false to actually write."),
    },
  },
  async (args) => {
    const a = [];
    pushFlag(a, "add-domain-marketing", args.add_domain_marketing);
    pushFlag(a, "remove-domain-marketing", args.remove_domain_marketing);
    pushFlag(a, "add-domain-personnel", args.add_domain_personnel);
    pushFlag(a, "add-pattern-actionnable", args.add_pattern_actionnable);
    pushFlag(a, "add-pattern-promotionnel", args.add_pattern_promotionnel);
    pushFlag(a, "set-weight", args.set_weight);
    pushFlag(a, "set-seuil", args.set_seuil);
    if (args.dry_run === false) a.push("--apply");
    return runAndFormat("config", "scoring-set", a);
  }
);

server.registerTool(
  "connaissance_config_scoring_diff",
  {
    description: "Diff between the user scoring config and the template.",
    inputSchema: {},
    annotations: { readOnlyHint: true },
  },
  async () => runAndFormat("config", "scoring-diff", [])
);

server.registerTool(
  "connaissance_config_scoring_validate",
  {
    description: "Validate that scoring-courriels.yaml is well-formed (valid regex, coherent thresholds).",
    inputSchema: {},
    annotations: { readOnlyHint: true },
  },
  async () => runAndFormat("config", "scoring-validate", [])
);

// ── manifest patching ──────────────────────────────────────────

server.registerTool(
  "connaissance_manifest_patch",
  {
    description: "Apply patches to an organization manifest. Supports targeted patches (by id) and bulk filter/set operations.",
    inputSchema: {
      manifest: z.string().describe("Absolute path to the manifest JSON."),
      patches: z.array(z.object({
        id: z.string().optional(),
        resume_path: z.string().optional(),
        set: z.record(z.string(), z.any()).optional(),
        delete: z.boolean().optional(),
      })).optional().describe("List of targeted patches."),
      filter: z.string().optional().describe("k1=v1,k2=v2 predicate for bulk patch."),
      set: z.string().optional().describe("k1=v1,k2=v2 values to apply to matched rows."),
      delete_filter: z.string().optional().describe("k1=v1,k2=v2 predicate for bulk delete."),
    },
  },
  async (args) => {
    const a = [args.manifest];
    if (args.patches) a.push("--patches", JSON.stringify(args.patches));
    pushFlag(a, "filter", args.filter);
    pushFlag(a, "set", args.set);
    pushFlag(a, "delete-filter", args.delete_filter);
    return runAndFormat("manifest", "patch", a);
  }
);

// ── Start stdio ────────────────────────────────────────────────

const transport = new StdioServerTransport();
await server.connect(transport);
