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
import { existsSync, mkdirSync } from "node:fs";
import { randomBytes } from "node:crypto";

/**
 * Dossier de transit persistant. Auparavant les fichiers générés (scans,
 * requests, résultats) étaient écrits dans `/tmp/` — purgé aléatoirement
 * par macOS entre sessions. Quand un batch Anthropic prend plusieurs
 * heures, le fichier de requests pouvait disparaître avant la fin du
 * batch, cassant `summarize_register` par manque de mapping
 * custom_id → source_path.
 *
 * Emplacement standard par plateforme :
 * - macOS : `~/Library/Application Support/connaissance/transit/`
 * - Linux (cowork VM) : `~/.local/share/connaissance/transit/`
 *
 * Distinct de `~/Connaissance/.config/` qui reste couplé à la base
 * (tracking DB, filtres, scoring — partent ensemble avec une
 * sauvegarde).
 */
function transitDir() {
  const isMac = process.platform === "darwin";
  const base = isMac
    ? join(homedir(), "Library", "Application Support", "connaissance")
    : join(
        process.env.XDG_DATA_HOME || join(homedir(), ".local", "share"),
        "connaissance",
      );
  const dir = join(base, "transit");
  mkdirSync(dir, { recursive: true });
  return dir;
}

/**
 * Génère un chemin persistant unique pour l'option `output_file` quand
 * l'appelant n'en fournit pas. Dossier : `~/Connaissance/.config/transit/`.
 * Format : `<kind>_<timestamp>_<id>.json`.
 */
function autoOutputFile(kind) {
  const id = randomBytes(4).toString("hex");
  const stamp = new Date().toISOString().replace(/[-:.]/g, "").slice(0, 15);
  return join(transitDir(), `${kind}_${stamp}_${id}.json`);
}

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

async function runCli(group, verb, args = [], opts = {}) {
  const fullArgs = [group, verb, ...args];
  const stdinPayload = opts.stdin;
  try {
    const childOpts = {
      env: { ...process.env },
      maxBuffer: 100 * 1024 * 1024, // 100 MB for large payloads (prompts, large extracts)
      timeout: 600_000, // 10 minutes (emails extract can be long)
    };
    let stdout;
    if (typeof stdinPayload === "string") {
      // execFileAsync does not expose stdin, so use spawn-like form via
      // execFile's callback API wrapped manually.
      stdout = await new Promise((resolve, reject) => {
        const child = execFile(CLI, fullArgs, childOpts, (err, out, errOut) => {
          if (err) {
            err.stderr = errOut;
            reject(err);
          } else {
            resolve(out);
          }
        });
        child.stdin.end(stdinPayload);
      });
    } else {
      ({ stdout } = await execFileAsync(CLI, fullArgs, childOpts));
    }
    // Si on arrive ici, le CLI a exit avec code 0 — stdout contient le JSON
    // attendu. stderr peut contenir des logs de progression ("N messages
    // hors plage ignorés via bisect..."), des rapports humains de calibrage,
    // etc. — ce ne sont pas des erreurs, on les ignore silencieusement.
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
    // Exit code non-zéro : essayer d'extraire un message d'erreur structuré
    // depuis stderr (le CLI émet du JSON {error: ...} sur stderr en cas
    // d'échec). Fallback : texte brut de stderr puis message natif.
    const stderrText = (err.stderr || "").trim();
    if (stderrText) {
      try {
        const parsed = JSON.parse(stderrText);
        throw new Error(parsed?.error?.message || parsed?.error || stderrText);
      } catch (e) {
        if (e instanceof SyntaxError) throw new Error(stderrText);
        throw e;
      }
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
async function runAndFormat(group, verb, args, opts) {
  try {
    const data = await runCli(group, verb, args, opts);
    return asToolResult(data);
  } catch (err) {
    return errorResult(`${group} ${verb} failed: ${safeError(err)}`);
  }
}

// ── pipeline ───────────────────────────────────────────────────

server.registerTool(
  "connaissance_pipeline_detect",
  {
    description: "Detect outstanding pipeline work : missing summaries, unorganized summaries, stale syntheses, stale MOCs, cost estimates, DB stats. " +
      "When the user asks about a specific time window (« pour 2026 », « depuis mars », etc.), ALWAYS pass 'since'/'until' — otherwise the backlog shown includes the entire history and the numbers will be misleading.",
    inputSchema: {
      steps: z.string().optional().describe("Comma-separated subset of: resumes_manquants, resumes_perimes, non_organises, synthese_perimee, moc_perimes, couts, stats. Default 'all'."),
      source: z.enum(["document", "courriel", "note"]).optional().describe("Filter by source type."),
      mode: z.enum(["batch", "interactif"]).default("batch").describe("Cost estimation mode."),
      ...dateRangeSchema,
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => {
    const a = [];
    pushFlag(a, "steps", args.steps);
    pushFlag(a, "source", args.source);
    pushFlag(a, "mode", args.mode);
    pushFlag(a, "since", args.since);
    pushFlag(a, "until", args.until);
    return runAndFormat("pipeline", "detect", a);
  }
);

server.registerTool(
  "connaissance_pipeline_costs",
  {
    description: "Pipeline cost in USD. Two modes: (default) forecast estimate for the current backlog (missing summaries, stale entities, stale MOCs), or 'real=true' to aggregate actually-measured usage from the llm_usage journal (tokens in/out, cache hit rate, cost per source_type and per model). " +
      "Accepts 'since'/'until' to scope the window — always pass them when the user asks about a specific period.",
    inputSchema: {
      mode: z.enum(["batch", "interactif"]).default("batch").describe("Batch API gets 50% discount vs interactive. Ignored when real=true."),
      real: z.boolean().optional().describe("When true, return real measured costs from the llm_usage journal instead of a forecast. Use to calibrate model routing and measure cache effectiveness."),
      ...dateRangeSchema,
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => {
    const a = ["--mode", args.mode ?? "batch"];
    if (args.real) a.push("--real");
    pushFlag(a, "since", args.since);
    pushFlag(a, "until", args.until);
    return runAndFormat("pipeline", "costs", a);
  }
);

server.registerTool(
  "connaissance_pipeline_simulate",
  {
    description: "Composite dry-run covering BOTH (a) the DB backlog — transcriptions already present that need summaries/syntheses — and (b) the SOURCES — documents, emails and notes on disk/IMAP/Apple Notes that have not been imported yet. Returns {detect, costs, sources_to_transcribe: {documents, courriels, notes}}. " +
      "This is the canonical tool to answer « qu'y a-t-il à faire sur la base ? » or « y a-t-il des notes/courriels à transcrire ? » — relying on detect alone misses the sources-side backlog (they aren't in the DB until transcribed). " +
      "When the user scopes the update to a time window (« pour 2026 »), ALWAYS pass 'since'/'until' — otherwise the preview will show the full historical backlog and lead to a wrong next step.",
    inputSchema: {
      mode: z.enum(["batch", "interactif"]).default("batch"),
      ...dateRangeSchema,
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => {
    const a = ["--mode", args.mode ?? "batch"];
    pushFlag(a, "since", args.since);
    pushFlag(a, "until", args.until);
    return runAndFormat("pipeline", "simulate", a);
  }
);

// ── documents ──────────────────────────────────────────────────

server.registerTool(
  "connaissance_documents_scan",
  {
    description: "Scan ~/Documents/ and list files to transcribe. Applies filtres.yaml (extensions, excluded folders, date range). " +
      "The full scan is always written to a JSON file (auto-generated path by default). The response contains compact metadata (total_to_transcribe, by_year, sample_to_transcribe) — enough to decide the next step without opening the file. " +
      "When the full list is needed (e.g. to submit a batch OCR), pass the returned 'output_file' to a downstream tool that reads files directly — such as `mistral-ocr ocr_batch_submit(files_from_json=...)`. Never try to `bash cat` or `python open()` the file from a Claude sandbox — the sandbox doesn't see the host filesystem. Use the `Read` MCP tool if you must inspect contents.",
    inputSchema: {
      ...dateRangeSchema,
      output_file: z.string().optional().describe(
        "Absolute path where the full scan JSON will be written. Default : " +
        "auto-generated temp path. The response always contains 'output_file'."
      ),
      inline: z.boolean().optional().describe(
        "Escape hatch : if true, return the full scan inline (may exceed 1 MB). " +
        "Not recommended — prefer the downstream tool that reads the file directly."
      ),
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => {
    const a = [];
    pushFlag(a, "since", args.since);
    pushFlag(a, "until", args.until);
    const outputFile = args.inline === true
      ? undefined
      : (args.output_file || autoOutputFile("documents_scan"));
    pushFlag(a, "output-file", outputFile);
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
    description: "Scan ~/Notes/ and list Apple Notes markdown files to copy into the knowledge base. " +
      "The full scan is always written to a JSON file (auto-generated path by default). The response contains compact metadata (total_to_copy, by_year, sample_to_copy). Never bash/python the file from a sandbox — use the `Read` MCP tool or pass the 'output_file' to a downstream tool that reads it directly.",
    inputSchema: {
      ...dateRangeSchema,
      output_file: z.string().optional().describe(
        "Absolute path where the full scan JSON will be written. Default : " +
        "auto-generated temp path. The response always contains 'output_file'."
      ),
      inline: z.boolean().optional().describe(
        "Escape hatch : if true, return the full scan inline (can exceed 700 KB). " +
        "Not recommended."
      ),
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => {
    const a = [];
    pushFlag(a, "since", args.since);
    pushFlag(a, "until", args.until);
    const outputFile = args.inline === true
      ? undefined
      : (args.output_file || autoOutputFile("notes_scan"));
    pushFlag(a, "output-file", outputFile);
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
      "Returns compact metadata {output_file, total, estimated_input_tokens, " +
      "source_types, total_bytes}; the full requests (with system/user prompts) " +
      "are written to a JSON file. By default a temp path is auto-generated so " +
      "the prompts NEVER enter the assistant context — pass 'output_file' to " +
      "choose a specific path, or 'inline=true' to get the old inline response " +
      "(not recommended, easily saturates the context for 10+ requests). " +
      "Typical flow: " +
      "summarize_prepare() → {output_file: '/tmp/...'} → submit_batch(requests_file='/tmp/...'). " +
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
      preference: z.enum(["auto", "quality", "economy"]).optional().describe(
        "Model routing preference. 'auto' (default) dispatches each request to " +
        "Sonnet or Haiku via the central heuristic (short emails/notes → Haiku, " +
        "old sources > 18 months → Haiku, long documents and threads → Sonnet). " +
        "'economy' forces Haiku except where it degrades (long documents, " +
        "threads). 'quality' forces Sonnet except for trivial short notes. " +
        "Propose 'economy' when the user is running a large retroactive batch " +
        "of old documents/emails where the marginal quality of Sonnet is not " +
        "worth the cost."
      ),
      output_file: z.string().optional().describe(
        "Absolute path where the full requests JSON will be written. Default: " +
        "auto-generated temp path. The response always contains 'output_file' " +
        "so you know where the file is."
      ),
      inline: z.boolean().optional().describe(
        "Escape hatch: if true, return the full {requests: [...]} inline " +
        "instead of writing to a file. Not recommended — even 10 requests " +
        "can exceed 50 KB of prompt text that pollutes the assistant context. " +
        "Leave unset unless you really want the old behaviour."
      ),
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
    pushFlag(a, "preference", args.preference);
    // Default : auto-generated output_file so prompts never enter the
    // assistant context. Only skip when the caller explicitly asks
    // for inline output (escape hatch).
    const outputFile = args.inline === true
      ? undefined
      : (args.output_file || autoOutputFile("summarize_prepare"));
    pushFlag(a, "output-file", outputFile);
    return runAndFormat("summarize", "prepare", a);
  }
);

server.registerTool(
  "connaissance_summarize_register",
  {
    description:
      "Post-process a summary returned by claude-api-mcp: parse the frontmatter, derive the destination path, write to Résumés/, update tracking.db. " +
      "Two modes : (a) single — pass {custom_id, content} for one summary ; " +
      "(b) batch — pass only {from_results_file} pointing at the output of " +
      "`claude_api wait_for_batch` or `query_direct` with output_file, and all " +
      "summaries are registered in one call without loading their contents into " +
      "the caller's context.",
    inputSchema: {
      custom_id: z.string().optional().describe("Custom ID (single mode only)."),
      content: z.string().optional().describe("Full markdown content with frontmatter (single mode only)."),
      source_path: z.string().optional().describe("Fallback source path if content frontmatter is missing."),
      from_results_file: z
        .string()
        .optional()
        .describe(
          "Batch mode: JSON file {results: [{custom_id, content, ...}]} from " +
          "claude-api-mcp. All entries are registered in one pass. Preferred for " +
          "API-based workflows — no content transits through the MCP channel."
        ),
      requests_file: z
        .string()
        .optional()
        .describe(
          "Batch mode (optional but strongly recommended): path to the " +
          "prep file produced by summarize_prepare(output_file=...). Used as " +
          "a fallback to resolve 'source_path' by custom_id when the LLM " +
          "forgot to inject `source:` in the generated frontmatter — which " +
          "is frequent enough to make this flag almost mandatory. Without " +
          "it, a single forgetful batch will fail every item with " +
          "« pas de champ source dans le frontmatter »."
        ),
      no_cleanup: z
        .boolean()
        .optional()
        .describe(
          "Conserver les fichiers de transit (from_results_file, " +
          "requests_file sous /tmp/) après l'enregistrement. Par défaut ils " +
          "sont supprimés si aucune erreur — c'est du cache temporaire."
        ),
    },
  },
  async (args) => {
    if (args.from_results_file) {
      const a = ["--from-results-file", args.from_results_file];
      if (args.requests_file) a.push("--requests-file", args.requests_file);
      if (args.no_cleanup) a.push("--no-cleanup");
      return runAndFormat("summarize", "register", a);
    }
    if (!args.custom_id || !args.content) {
      throw new Error(
        "connaissance_summarize_register: must pass either {custom_id, content} (single mode) or {from_results_file} (batch mode)."
      );
    }
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
  "connaissance_synthesis_list_all",
  {
    description: "Return a full inventory of Synthèse/: all fiches (personnes + organismes) with parsed frontmatter (aliases, status, first/last-contact, relations), MOCs (sujets), and recent digests. Use for the dashboard skill to avoid Glob patterns hitting the NFC/NFD Unicode normalization mismatch on macOS (folder names like 'Synthèse' are NFD on disk). Python reads the filesystem directly here.",
    inputSchema: {},
    annotations: { readOnlyHint: true },
  },
  async () => runAndFormat("synthesis", "list-all", [])
);

server.registerTool(
  "connaissance_synthesis_entity_paths",
  {
    description: "Return the canonical Résumés/ folder paths for a given entity — only folders that actually exist on disk. Use this to build the 'Liens' section of fiches deterministically, avoiding LLM hallucinations of wrong capitalization or non-existent subfolders.",
    inputSchema: {
      entity: z.string().describe("Entity identifier in 'type/slug' format (e.g., 'organismes/revenu-quebec')."),
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => runAndFormat("synthesis", "entity-paths", ["--entity", args.entity])
);

server.registerTool(
  "connaissance_synthesis_register",
  {
    description: "Write a fiche / chronologie / MOC / digest / index and register it in tracking.db. " +
      "Single mode: pass {content, kind, entity} to write ONE file. " +
      "Batch mode: pass {from_results_file, requests_file} to register many fiche+chronologie pairs at once from an API results file produced by claude_api__wait_for_batch or query_direct — the content is split on <!-- FICHE --> / <!-- CHRONOLOGIE --> markers and nothing transits through the MCP channel. Preferred for API-generated batches. " +
      "The destination path is computed from `kind` + `entity` so Claude never needs to know the knowledge base root (which differs between native and cowork VM).",
    inputSchema: {
      content: z.string().optional().describe("Single mode: markdown content to write. Must include YAML frontmatter matching the template for the given kind."),
      kind: z.enum(["fiche", "chronologie", "moc", "digest", "index"]).optional().describe(
        "Single mode: type of synthesis output. "
        + "fiche/chronologie → Synthèse/{entity_type}/{entity_slug}/{kind}.md ; "
        + "moc → Synthèse/sujets/{entity}.md ; "
        + "digest → Synthèse/rapports/digests/{entity or today}.md ; "
        + "index → Synthèse/index.md"
      ),
      entity: z.string().optional().describe(
        "Single mode. Required for fiche/chronologie (format 'type/slug', e.g. 'personnes/jean-dupont'). "
        + "Required for moc (category slug, e.g. 'banque'). "
        + "Optional for digest (date YYYY-MM-DD, default today). Ignored for index."
      ),
      source_type: z.enum(["document", "courriel", "note", "synthese"]).optional().describe("Optional: origin category of the primary source that triggered this update (for tracking only)."),
      source_path: z.string().optional().describe("Optional: path of a resume that triggered this synthesis (for tracking only)."),
      from_results_file: z.string().optional().describe(
        "Batch mode: JSON file {results: [{custom_id, content, ...}]} from claude-api-mcp. " +
        "Each content is split on <!-- FICHE --> / <!-- CHRONOLOGIE --> and registered as a fiche+chronologie pair."
      ),
      requests_file: z.string().optional().describe(
        "Batch mode (required with from_results_file): prep file produced by synthesis_prepare(output_file=...). " +
        "Supplies the custom_id → entity mapping that API results don't carry."
      ),
      no_cleanup: z.boolean().optional().describe("Batch mode: keep the transit files after registration (default: delete if no errors)."),
    },
  },
  async (args) => {
    if (args.from_results_file) {
      const a = ["--from-results-file", args.from_results_file];
      if (args.requests_file) a.push("--requests-file", args.requests_file);
      if (args.no_cleanup) a.push("--no-cleanup");
      return runAndFormat("synthesis", "register", a);
    }
    const a = ["--kind", args.kind];
    if (args.entity) a.push("--entity", args.entity);
    if (args.source_type) a.push("--source-type", args.source_type);
    if (args.source_path) a.push("--source-path", args.source_path);
    // Pass content via stdin to avoid argv size limits and shell escaping.
    a.push("--content-stdin");
    return runAndFormat("synthesis", "register", a, { stdin: args.content });
  }
);

server.registerTool(
  "connaissance_synthesis_prepare",
  {
    description: "Build LLM requests (fiche + chronologie) for stale entities. " +
      "Symmetric to summarize_prepare — the generation moves OUT of the main Claude context and into the Anthropic API (batch for -50%, or direct), unloading the summaries from the principal's window. " +
      "Returns compact metadata {output_file, total, estimated_input_tokens, model_tiers}; the full requests are written to a JSON file. Typical flow: synthesis_prepare() → submit_batch(requests_file=...) → wait_for_batch(output_file=...) → synthesis_register(from_results_file=...). " +
      "The central model heuristic routes each entity to Sonnet or Haiku (see preference).",
    inputSchema: {
      entities: z.union([z.string(), z.array(z.string())]).optional().describe(
        "'type/slug,type/slug,…' or array. Omit to target all stale entities from synthesis_plan()."
      ),
      preference: z.enum(["auto", "quality", "economy"]).optional().describe(
        "Model routing. 'auto' (default): Sonnet for fiche/chronologie (narrative). " +
        "'economy': Haiku — propose it for massive retroactive rewrites where Sonnet quality is not worth the cost. " +
        "'quality': force Sonnet everywhere."
      ),
      output_file: z.string().optional(),
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => {
    const a = [];
    let entsVal = args.entities;
    if (Array.isArray(entsVal)) entsVal = entsVal.join(",");
    pushFlag(a, "entities", entsVal);
    pushFlag(a, "preference", args.preference);
    const outputFile = args.output_file || autoOutputFile("synthesis_prepare");
    pushFlag(a, "output-file", outputFile);
    return runAndFormat("synthesis", "prepare", a);
  }
);

// ── audit ──────────────────────────────────────────────────────

server.registerTool(
  "connaissance_audit_check",
  {
    description: "Run deterministic integrity checks : broken links, invalid frontmatter, desynchronized triplets, missing attachments, duplicates. For overdue actions (business content), use connaissance_actions_list instead.",
    inputSchema: {
      steps: z.string().optional().describe("Comma-separated subset of: liens_casses, frontmatter_invalide, triplets_desynchronises, attachements_manquants, doublons. Default 'all'."),
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
  "connaissance_actions_list",
  {
    description: "List open action items extracted from entity chronologies (- [ ] checkboxes). Returns {items: [{entite, action, echeance, status, raison, source_path}], total}. Business content, not integrity.",
    inputSchema: {
      status: z.enum(["all", "ouverte", "expiree"]).default("all").describe("Filter by status. 'expiree' = overdue (échéance < today) or open > 90 days without update."),
      entity: z.string().optional().describe("Filter by entity identifier in 'type/slug' format (e.g., 'organismes/fmrq')."),
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => {
    const a = [];
    pushFlag(a, "status", args.status);
    pushFlag(a, "entity", args.entity);
    return runAndFormat("actions", "list", a);
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
