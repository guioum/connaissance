// MCP server wrapper for the `connaissance` CLI.
//
// Exposes 74 tools (mcp__connaissance__*) that shell-out to the
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
import { join, dirname } from "node:path";
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
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
const TRANSIT_DIR = (() => {
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
})();

/**
 * Génère un chemin persistant unique pour l'option `output_file` quand
 * l'appelant n'en fournit pas. Format : `<kind>_<timestamp>_<id>.json`.
 */
function autoOutputFile(kind) {
  const id = randomBytes(4).toString("hex");
  const stamp = new Date().toISOString().replace(/[-:.]/g, "").slice(0, 15);
  return join(TRANSIT_DIR, `${kind}_${stamp}_${id}.json`);
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
if (CLI === "connaissance") {
  process.stderr.write(
    "[connaissance-mcp] Warning: CLI binary not found at absolute path; " +
    "relying on PATH resolution. Set CONNAISSANCE_CLI or install via " +
    "`uv tool install git+https://github.com/guioum/connaissance` to suppress.\n"
  );
}

// Derive the server version from package.json so it tracks releases
// automatically rather than drifting as a hard-coded string.
const __pkg = JSON.parse(
  readFileSync(join(dirname(fileURLToPath(import.meta.url)), "package.json"), "utf-8")
);
const SERVER_VERSION = __pkg.version;

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
    try {
      return JSON.parse(stdout);
    } catch (parseErr) {
      // Un warning Python (ImportWarning, DeprecationWarning...) qui fuit
      // sur stdout avant le JSON déclenchera ici — surfacer un message
      // diagnostic plutôt qu'un `Unexpected token` opaque.
      throw new Error(
        `CLI stdout is not valid JSON (exit 0). First 200 chars: ${stdout.slice(0, 200)}`
      );
    }
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
  version: SERVER_VERSION,
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

// NOTE — pipeline_simulate was removed in v2.11.0. The aggregator ran
// documents_scan + notes_scan + emails_extract --dry-run + pipeline_detect
// + pipeline_costs sequentially server-side and routinely exceeded the
// 60s MCP client timeout on loaded bases. The skill `pipeline` composes
// these sub-tools in parallel instead — same coverage, no timeout risk,
// one source of truth.

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
  "connaissance_documents_backlog_count",
  {
    description: "FAST count of documents to transcribe under ~/Documents/ WITHOUT hashing any file. Walks the tree, applies the filter (extension, excluded folders, date range via mtime), and checks existence of the mirror transcription — no SHA256, no tracking.db dedup. Returns {total_to_transcribe, by_year, skipped}. Timeout-safe alternative to documents_scan for pipeline overviews. For an exact count (with source_changed detection and hash-level dedup), use documents_scan.",
    inputSchema: {
      ...dateRangeSchema,
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => {
    const a = [];
    pushFlag(a, "since", args.since);
    pushFlag(a, "until", args.until);
    return runAndFormat("documents", "backlog-count", a);
  }
);

server.registerTool(
  "connaissance_documents_register",
  {
    description: "Register a document transcription in tracking.db and inject canonical frontmatter (source, source_hash, transcribed_at). Pass ocr_engine='mistral' when registering a Mistral re-pass that replaces a vision-local transcription (provenance stamp).",
    inputSchema: {
      source_file: z.string().describe("Absolute path to the original document file (PDF, image, etc.)."),
      transcription: z.string().describe("Absolute path to the generated transcription markdown."),
      ocr_engine: z.string().optional().describe("OCR engine provenance ('mistral' / 'vision-local'). Stamped into frontmatter; omit to preserve the existing value."),
    },
  },
  async (args) => {
    const a = [args.source_file, args.transcription];
    if (args.ocr_engine) a.push("--ocr-engine", args.ocr_engine);
    return runAndFormat("documents", "register", a);
  }
);

server.registerTool(
  "connaissance_documents_transcribe_plan",
  {
    description: "Mistral re-pass WORKLIST, page-bounded for cost. DB-driven (doc_signals). Lists documents that need OCR (scanned PDFs, image-documents) and do NOT yet have a Mistral transcription — either a vision-local transcription to UPGRADE to Mistral's structured markdown, or (by default) a scanned doc with no transcription at all. Born-digital PDFs (clean text layer) are excluded and counted (born_digital_skip) unless include_born_digital=true (single engine/format for the whole base; counted born_digital_included). Phantom duplicate rows (NFC/NFD/case variants of the same file) are deduped (counts.phantom_dupes). Documents over max_pages go to 'deferred'. Returns counts, estimated_pages and estimated_cost_usd ($1/1000 pages), plus to_transcribe[] (each item carries source canonical + read_source SSD mirror + transcription target). Read-only except --output_file. INTENDED FLOW: call with output_file to write a manifest, then mistral-ocr ocr_batch_submit(files_from_json=manifest, preserve_paths=~/Documents) → ocr_batch_results(output=Transcriptions/Documents) → documents_register_batch(from_scan=manifest, ocr_engine='mistral'). The manifest's read_source makes mistral-ocr read from the SSD mirror (no iCloud download). Requires up-to-date signals (documents signals, schema v>=6) for pages + ocr_cache.",
    inputSchema: {
      max_pages: z.number().optional().describe("Page ceiling for the re-pass (default 10). Docs above go to deferred."),
      scope: z.string().optional().describe("Restrict to a subfolder (relative to ~/Documents)."),
      upgrade_only: z.boolean().optional().describe("Only upgrade existing vision-local transcriptions; exclude scanned docs with no transcription."),
      include_born_digital: z.boolean().optional().describe("Also include born-digital PDFs (default false: clean text layer, no OCR needed). Use to unify the whole base on Mistral markdown."),
      output_file: z.string().optional().describe("Write the to_transcribe manifest here (consumable by mistral-ocr files_from_json and register-batch). Without it, the full list returns inline."),
    },
  },
  async (args) => {
    const a = [];
    if (args.max_pages != null) a.push("--max-pages", String(args.max_pages));
    if (args.scope) a.push("--scope", args.scope);
    if (args.upgrade_only) a.push("--upgrade-only");
    if (args.include_born_digital) a.push("--include-born-digital");
    if (args.output_file) a.push("--output-file", args.output_file);
    return runAndFormat("documents", "transcribe-plan", a);
  }
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
  "connaissance_documents_register_batch",
  {
    description: "Batch-register a scan/transcribe-plan manifest : read the output-file (to_transcribe items carry source + transcription paths) and register every document whose transcription now exists on disk, reusing the exact paths computed at plan time. Missing transcriptions are reported loudly under `missing` (e.g. an OCR batch written outside Transcriptions/Documents/) instead of producing silent orphans. Use after an OCR batch instead of calling register per file. Pass ocr_engine='mistral' to stamp provenance when registering a Mistral re-pass (from documents_transcribe_plan).",
    inputSchema: {
      from_scan: z.string().describe("Path to the JSON manifest produced by `documents scan --output-file` or `documents transcribe-plan --output-file`."),
      dry_run: z.boolean().optional().describe("Report what would be registered (and what's missing) without writing."),
      ocr_engine: z.string().optional().describe("OCR engine provenance stamped into each transcription's frontmatter (e.g. 'mistral')."),
    },
  },
  async (args) => {
    const a = ["--from-scan", args.from_scan];
    if (args.dry_run) a.push("--dry-run");
    if (args.ocr_engine) a.push("--ocr-engine", args.ocr_engine);
    return runAndFormat("documents", "register-batch", a);
  }
);

server.registerTool(
  "connaissance_documents_category_view",
  {
    description: "Generate a category-based VIEW of ~/Documents/ as symlinks under '- Par catégorie/<category>/'. The canonical tree stays organized by entity; the category lives in metadata. Read-only on the originals, fully regenerable, and excluded from the pipeline scan via the '- ' prefix. Default is dry-run (returns the per-category breakdown). Pass apply=true to (re)build the symlinks, clear=true to remove the view. Regenerate after a classification session, since it's a snapshot.",
    inputSchema: {
      apply: z.boolean().optional().describe("(Re)build the symlink view (idempotent)."),
      clear: z.boolean().optional().describe("Remove the view (reversible)."),
    },
  },
  async (args) => {
    const a = [];
    if (args.apply) a.push("--apply");
    if (args.clear) a.push("--clear");
    return runAndFormat("documents", "category-view", a);
  }
);

server.registerTool(
  "connaissance_documents_triage",
  {
    description: "Phase A of the ~/Documents reorganization : map the whole folder into 4 groups (A real documents, B app exports, C media, D code) WITHOUT OCR, read-only. Detects containers (code repos via marker files, .app bundles, export folders) and counts them as single units instead of enumerating every file inside — a code repo stays grouped. Returns the breakdown + the list of repos/bundles/exports + a sample of real documents. Pass output_file to write the full report to disk.",
    inputSchema: {
      output_file: z.string().optional().describe("Write the full JSON report to this path instead of inline."),
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => {
    const a = [];
    if (args.output_file) a.push("--output-file", args.output_file);
    return runAndFormat("documents", "triage", a);
  }
);

server.registerTool(
  "connaissance_documents_secrets",
  {
    description: "Scan ~/Documents (read-only, no OCR, no network) for files containing SECRETS — keys, passwords, tokens — so they can be QUARANTINED (never classified in clear, indexed in qmd, or sent to an external service like Mistral OCR / Batch API). Lightweight secret-scanning (known token prefixes AKIA/ghp_/sk-/AIza/xox…, PEM private-key blocks, JWT, user:pass@host URLs, high-entropy `password=`/`api_key=` assignments, CSV password columns) plus sensitive filenames (.env, id_rsa, *.pem, *.pfx, *.kdbx, credentials.*). Reads content via the SSD mirror only — never triggers an iCloud download (dataless files get filename-signal only). Matches are always redacted. NOT a PII detector. Pass scope to restrict to a subfolder; output_file to write the full report.",
    inputSchema: {
      scope: z.string().optional().describe("Restrict to a subfolder of ~/Documents (relative path, e.g. 'Classer/old')."),
      output_file: z.string().optional().describe("Write the full JSON report to this path instead of inline."),
      quarantine: z.boolean().optional().describe("ACTIVE guard: write the detected files into the quarantine list (~/Connaissance/.config/secrets-quarantine.txt) so they are EXCLUDED from OCR, the qmd index and the Batch API (filtres rejects them, reason 'secret_quarantine'). Writes a config file only — moves/deletes nothing."),
      include_medium: z.boolean().optional().describe("With quarantine: also add 'medium' detections (default: high only)."),
      relocate: z.boolean().optional().describe("Physically MOVE quarantined secrets to ~/Documents/- Protégés/secrets/ via the ledger (reversible). Distinct from the active guard. Dry-run unless apply=true."),
      apply: z.boolean().optional().describe("With relocate: actually move (default: dry-run)."),
    },
  },
  async (args) => {
    const a = [];
    if (args.relocate) {
      a.push("--relocate");
      if (args.apply) a.push("--apply");
      return runAndFormat("documents", "secrets", a);
    }
    if (args.scope) a.push("--scope", args.scope);
    if (args.quarantine) {
      a.push("--quarantine");
      if (args.include_medium) a.push("--include-medium");
    } else if (args.output_file) {
      a.push("--output-file", args.output_file);
    }
    return runAndFormat("documents", "secrets", a);
  }
);

server.registerTool(
  "connaissance_documents_signals",
  {
    description: "Phase B of the ~/Documents reorganization : extract a 'signal packet' per real document (group A) WITHOUT OCR, read-only, to feed later heuristic pre-classification. Cheap-to-expensive cascade: name + path + filesystem dates + type hint (stdlib) → Office metadata (docProps) → existing OCR-cache text → Office/plain text → PDF born-digital text layer page 1 (pypdfium2 if installed, last resort). Detects born-digital vs scanned. Produces keywords + extractive summary (Luhn) + entities (amounts/dates/refs). Skips containers, skips secret/quarantined files, reads via the SSD mirror (dataless files get name/path signals only — never an iCloud download), and caches packets in tracking.db. Pass output_file (the report can be large). Pass scope to restrict to a subfolder.",
    inputSchema: {
      scope: z.string().optional().describe("Restrict to a subfolder of ~/Documents (relative path)."),
      output_file: z.string().optional().describe("Write the full JSON report to this path instead of inline (recommended — large on a big corpus)."),
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => {
    const a = [];
    if (args.scope) a.push("--scope", args.scope);
    if (args.output_file) a.push("--output-file", args.output_file);
    return runAndFormat("documents", "signals", a);
  }
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
  "connaissance_documents_ocr_review",
  {
    description: "List OCR transcriptions whose confidence score is at or below a threshold — a quality flag for human review BEFORE trusting them in classification/summaries. Reads ocr_confidence (the minimum page score) from the transcription frontmatter; transcriptions without a score (OCR'd without confidence_scores) are skipped. Defaults to Mistral transcriptions (the terminal engine: no re-pass beyond, but a low score means a dubious OCR). Read-only.",
    inputSchema: {
      max_confidence: z.number().default(0.85).describe("List transcriptions with ocr_confidence <= this threshold (default 0.85)."),
      engine: z.string().default("mistral").describe('Filter by OCR engine (mistral, vision-local) or "all" for any engine (default mistral).'),
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => {
    const a = [];
    if (args.max_confidence != null) a.push("--max-confidence", String(args.max_confidence));
    if (args.engine) a.push("--engine", args.engine);
    return runAndFormat("documents", "ocr-review", a);
  }
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
    description: "Count emails per mbox folder without extracting, but STILL PARSES each message (slow on loaded accounts — can exceed MCP 60s timeout). For a fast overview backlog, prefer `connaissance_emails_backlog_count`.",
    inputSchema: emailsCommonSchema,
    annotations: { readOnlyHint: true },
  },
  async (args) => runAndFormat("emails", "stats", emailsCommonArgs(args))
);

server.registerTool(
  "connaissance_emails_backlog_count",
  {
    description: "FAST email backlog count per mbox folder using only the imap-backup .imap index (no message parsing, no scoring, no dedup vs tracking.db). Returns an upper bound of extractable emails per folder within the date range. Designed as a timeout-safe alternative to emails_stats / emails_extract --dry-run for pipeline overviews. For an exact count with scoring, use emails_extract --dry-run.",
    inputSchema: emailsCommonSchema,
    annotations: { readOnlyHint: true },
  },
  async (args) => runAndFormat("emails", "backlog-count", emailsCommonArgs(args))
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
    // dry_run=true est le défaut argparse du CLI. On pousse --apply pour le flipper.
    if (args.dry_run === false) a.push("--apply");
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
  "connaissance_notes_backlog_count",
  {
    description: "FAST count of Apple Notes to copy/update from ~/Notes/ WITHOUT reading any file contents. Walks the tree, filters by file mtime (approximation of frontmatter `created`), and checks destination mirror existence + mtime. Returns {total_to_copy, to_copy, to_update, skipped_total}. Timeout-safe alternative to notes_scan for pipeline overviews. Trade-off: the date filter uses mtime instead of the frontmatter `created` field (an old note modified recently is counted as recent). For exact frontmatter-based filtering, use notes_scan.",
    inputSchema: {
      ...dateRangeSchema,
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => {
    const a = [];
    pushFlag(a, "since", args.since);
    pushFlag(a, "until", args.until);
    return runAndFormat("notes", "backlog-count", a);
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
  async (args) => runAndFormat(
    "organize", "enrich",
    [args.manifest, "--qmd-results-stdin"],
    { stdin: JSON.stringify(args.qmd_results) },
  )
);

server.registerTool(
  "connaissance_organize_apply",
  {
    description: "Apply an organization manifest : move summaries, transcriptions, original documents to their entity directories. Always dry-run first.",
    inputSchema: {
      manifest: z.string().describe("Absolute path to the manifest JSON."),
      dry_run: z.boolean().default(true).describe("Default dry-run ; pass false to actually move files."),
    },
  },
  async (args) => {
    const a = [args.manifest];
    // dry_run=true est le défaut argparse du CLI. On pousse --apply pour le flipper.
    if (args.dry_run === false) a.push("--apply");
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
      dry_run: z.boolean().default(true).describe("Default dry-run ; pass false to actually promote/dedup."),
    },
  },
  async (args) => {
    const a = [];
    // dry_run=true est le défaut argparse du CLI. On pousse --apply pour le flipper.
    if (args.dry_run === false) a.push("--apply");
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
  "connaissance_classify_prepare",
  {
    description:
      "Phase C (pre-classification of ~/Documents) — build Batch API requests to classify documents from their Phase-B SIGNAL packets (NOT the full document). OFFLINE: generates requests, submits nothing. Each request bundles the signal packet + a free heuristic hint + the list of already-known entities, and asks Claude to return strict JSON {entity, entity_type, category, date, title, sujet, confidence, reason} — normalizing the entity against the known list (e.g. 'BNC' → 'Banque Nationale') and cleaning the title. Category is one of the canonical domains (banque, logement, impots, telecom, sante, ...). Writes requests to a transit file (and full requests to output_file) for submit_batch. Default model is Haiku 4.5 (cheap, short input). Typical flow: documents_signals(output_file) → classify_prepare(from_signals) → submit_batch → classify_register (later). Read-only on the corpus.",
    inputSchema: {
      scope: z.string().optional().describe("Subfolder of ~/Documents to classify (relative path)."),
      from_signals: z.string().optional().describe("Path to a `documents signals --output-file` JSON (avoids re-scanning)."),
      model: z.string().optional().describe("Batch model (default: claude-haiku-4-5-20251001)."),
      limit: z.number().optional().describe("Cap the number of documents (sampling)."),
      output_file: z.string().optional().describe("Write the full requests JSON to this path (recommended)."),
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => {
    const a = [];
    if (args.scope) a.push("--scope", args.scope);
    if (args.from_signals) a.push("--from-signals", args.from_signals);
    if (args.model) a.push("--model", args.model);
    if (args.limit != null) a.push("--limit", String(args.limit));
    if (args.output_file) a.push("--output-file", args.output_file);
    return runAndFormat("classify", "prepare", a);
  }
);

server.registerTool(
  "connaissance_classify_register",
  {
    description:
      "Phase C brique 4 — consume Batch classification results + the classify_prepare file, and build the plan→apply MANIFEST. For each doc: parse Claude's JSON, validate (canonical category, AAAA-MM-JJ date), reconcile the entity against the existing registry (resolution.py aliases/slug), and compute the destination path entity_type/slug/'AAAA-MM-JJ title.ext'. Low confidence, missing date, invalid category, divers entity, or parse failure → 'attente' (waiting zone, NOT moved). Writes a reviewable manifest (transit file + output_file) for classify apply. Moves/writes NOTHING to the corpus.",
    inputSchema: {
      results: z.string().describe("Path to the Batch results JSON (retrieve_batch_results / wait_for_batch output)."),
      from_prepare: z.string().describe("Path to the classify_prepare output file (source rel + hint per custom_id)."),
      output_file: z.string().optional().describe("Write the full manifest JSON to this path."),
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => {
    const a = ["--results", args.results, "--from-prepare", args.from_prepare];
    if (args.output_file) a.push("--output-file", args.output_file);
    return runAndFormat("classify", "register", a);
  }
);

server.registerTool(
  "connaissance_classify_apply",
  {
    description:
      "Phase C brique 5 — apply a classify_register manifest: move each 'auto' entry to its destination (entity_type/slug/'AAAA-MM-JJ title.ext') VIA THE LEDGER (journaled, reversible with ledger_revert). 'attente' entries are left in place. DRY-RUN BY DEFAULT — pass apply=true to actually move files. Name collisions are handled ((2),(3)…). Returns planned/moved counts + ledger_run when files moved.",
    inputSchema: {
      manifest: z.string().describe("Path to the classify_register manifest JSON."),
      apply: z.boolean().optional().describe("Execute the moves (default false = dry-run, moves nothing)."),
    },
    annotations: { readOnlyHint: false },
  },
  async (args) => {
    const a = [args.manifest];
    if (args.apply) a.push("--apply");
    return runAndFormat("classify", "apply", a);
  }
);

server.registerTool(
  "connaissance_classify_status",
  {
    description:
      "Identity card of the document pipeline (read-only). With 'path' (relative to ~/Documents): assembles a document's fiche — Phase-B signals + Phase-C classification (entity/category/date/title/sujet/confidence/status/model) + secrets-quarantine flag, joined from tracking.db. Without 'path': corpus-wide classification summary (counts by status auto/attente, category, entity_type, entity). Backed by the doc_signals + doc_classification tables that accumulate per file across stages and follow the file when it moves.",
    inputSchema: {
      path: z.string().optional().describe("Document path relative to ~/Documents for its full fiche. Omit for the corpus summary."),
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => {
    const a = [];
    if (args.path) a.push("--path", args.path);
    return runAndFormat("classify", "status", a);
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
      mode: z.enum(["batch", "direct"]).default("direct").describe(
        "Request format. Use 'direct' in all cases (including subagent processing). " +
        "'batch' adds cache_control headers for Anthropic's Message Batches API."
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
    pushFlag(a, "mode", args.mode ?? "direct");
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
      return errorResult(
        "connaissance_summarize_register: must pass either {custom_id, content} (single mode) or {from_results_file} (batch mode)."
      );
    }
    // Pass content via stdin — a summary markdown blob (5-15 KB of PII from
    // emails/docs) must never appear in process argv (visible in ps, logs).
    const a = [args.custom_id, "--stdin"];
    pushFlag(a, "source-path", args.source_path);
    return runAndFormat("summarize", "register", a, { stdin: args.content });
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
    // Single mode : kind AND content are required. Without this guard,
    // kind=undefined becomes the literal string "undefined" on argv (argparse
    // rejects it with a cryptic message) and content=undefined causes the
    // CLI to wait forever on --content-stdin (10-minute timeout).
    if (!args.kind || !args.content) {
      return errorResult(
        "connaissance_synthesis_register: single mode requires both {kind, content}. Use {from_results_file} for batch mode."
      );
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
    description: "Run deterministic integrity checks : broken links, invalid frontmatter, desynchronized triplets, missing attachments, exact duplicates, near-duplicate documents (SimHash on OCR text). For overdue actions (business content), use connaissance_actions_list instead.",
    inputSchema: {
      steps: z.string().optional().describe("Comma-separated subset of: liens_casses, frontmatter_invalide, triplets_desynchronises, attachements_manquants, doublons, quasi_doublons. Default 'all'."),
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
  "connaissance_audit_restore_journals",
  {
    description: "Rebuild the journal tables (file_ledger, llm_usage) from their append-only JSONL copies under .config/journal/ (recovery after DB loss). These are primary records — NOT derivable from the markdown frontmatter — so reindex-db cannot restore them; this command can. Without force: ledger adds only missing run_ids, usage imports only if its table is empty. With force: wipe then reimport.",
    inputSchema: {
      force: z.boolean().default(false).describe("Wipe then reimport (else: ledger adds missing runs, usage imports only if empty)."),
    },
  },
  async (args) => {
    const a = [];
    if (args.force) a.push("--force");
    return runAndFormat("audit", "restore-journals", a);
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
      dry_run: z.boolean().default(true).describe("Default dry-run ; pass false to actually move folders."),
    },
  },
  async (args) => {
    const a = [];
    // dry_run=true est le défaut argparse du CLI. On pousse --apply pour le flipper.
    if (args.dry_run === false) a.push("--apply");
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
      remove_domain_personnel: z.string().optional().describe("Comma-separated domains to remove from domaines_personnels."),
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
    pushFlag(a, "remove-domain-personnel", args.remove_domain_personnel);
    pushFlag(a, "add-pattern-actionnable", args.add_pattern_actionnable);
    pushFlag(a, "add-pattern-promotionnel", args.add_pattern_promotionnel);
    pushFlag(a, "set-weight", args.set_weight);
    pushFlag(a, "set-seuil", args.set_seuil);
    // dry_run=true is the CLI's argparse default. Push --apply only to flip it.
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
    pushFlag(a, "filter", args.filter);
    pushFlag(a, "set", args.set);
    pushFlag(a, "delete-filter", args.delete_filter);
    // Pass patches via stdin to avoid argv size / ps-ef leak on large batches.
    if (args.patches) {
      a.push("--patches-stdin");
      return runAndFormat("manifest", "patch", a, { stdin: JSON.stringify(args.patches) });
    }
    return runAndFormat("manifest", "patch", a);
  }
);

server.registerTool(
  "connaissance_ledger_list",
  {
    description: "List recent runs in the file-operation ledger (the reversible journal of every name/folder change). Each run groups the operations of one batch and can be reverted as a whole.",
    inputSchema: {
      limit: z.number().optional().describe("Max number of runs (default 20)."),
    },
    annotations: { readOnlyHint: true },
  },
  async (args) => {
    const a = [];
    if (args.limit != null) { a.push("--limit", String(args.limit)); }
    return runAndFormat("ledger", "list", a);
  }
);

server.registerTool(
  "connaissance_ledger_show",
  {
    description: "Show every operation (old path → new path, hash, reason, status) of a ledger run.",
    inputSchema: { run_id: z.string().describe("The run identifier.") },
    annotations: { readOnlyHint: true },
  },
  async (args) => runAndFormat("ledger", "show", [args.run_id])
);

server.registerTool(
  "connaissance_ledger_verify",
  {
    description: "Verify a ledger run against disk : are the moved files still present at their new path with the recorded hash? Reports any moved/modified/missing file. Read-only.",
    inputSchema: { run_id: z.string().describe("The run identifier.") },
    annotations: { readOnlyHint: true },
  },
  async (args) => runAndFormat("ledger", "verify", [args.run_id])
);

server.registerTool(
  "connaissance_ledger_revert",
  {
    description: "Roll back a ledger run : move each file back to its previous location, in reverse order. Hash-verified — a file whose content changed since the move is skipped, never overwritten. Pass dry_run=true to preview without moving anything.",
    inputSchema: {
      run_id: z.string().describe("The run identifier to revert."),
      dry_run: z.boolean().optional().describe("Preview the rollback without moving files."),
    },
  },
  async (args) => {
    const a = [args.run_id];
    if (args.dry_run) a.push("--dry-run");
    return runAndFormat("ledger", "revert", a);
  }
);

server.registerTool(
  "connaissance_ledger_purge",
  {
    description: "Empty the ledger trash : PERMANENTLY delete files that were sent to trash (op='trash', e.g. by optimize dedup/cleanup-orphans) instead of unlinked. Filterable by run and/or age. Irreversible. Dry-run by default — pass dry_run=false to actually delete.",
    inputSchema: {
      run_id: z.string().optional().describe("Limit to a single run_id (default: whole trash)."),
      older_than_days: z.number().int().optional().describe("Only purge entries older than N days."),
      dry_run: z.boolean().default(true).describe("Default dry-run ; pass false to actually delete."),
    },
  },
  async (args) => {
    const a = [];
    if (args.run_id) { a.push("--run", args.run_id); }
    if (args.older_than_days != null) { a.push("--older-than-days", String(args.older_than_days)); }
    // dry_run=true est le défaut argparse du CLI. On pousse --apply pour le flipper.
    if (args.dry_run === false) a.push("--apply");
    return runAndFormat("ledger", "purge", a);
  }
);

server.registerTool(
  "connaissance_ledger_snapshot",
  {
    description: "Build the ~/Documents/- Historique/ view: dated per-run snapshots reconstructing the OLD file structure/names BEFORE moves/renames, as symlinks pointing to each file's CURRENT location (move chain followed). Read-only, regenerable, cheap (symlinks from the ledger). Purged files → .disparu marker. Dry-run by default ; apply=true (re)builds, clear=true removes the view.",
    inputSchema: {
      run_id: z.string().optional().describe("Limit to a single run_id (default: all runs)."),
      apply: z.boolean().default(false).describe("(Re)build the - Historique view."),
      clear: z.boolean().default(false).describe("Remove the - Historique view."),
    },
  },
  async (args) => {
    const a = [];
    if (args.run_id) a.push("--run", args.run_id);
    if (args.clear) { a.push("--clear"); return runAndFormat("ledger", "snapshot", a); }
    if (args.apply) a.push("--apply");
    return runAndFormat("ledger", "snapshot", a);
  }
);

// ── Sujets (vue virtuelle « - Sujets ») ────────────────────────

server.registerTool(
  "connaissance_sujet_list",
  {
    description: "List subjects (sujets) and document counts, read from doc_classification.sujet (the single source of truth — no frontmatter on raw PDFs). Read-only.",
    inputSchema: {},
    annotations: { readOnlyHint: true },
  },
  async () => runAndFormat("sujet", "list", [])
);

server.registerTool(
  "connaissance_sujet_view",
  {
    description: "(Re)generate the navigable per-subject view ~/Documents/- Sujets/ as symlinks pointing to each document's current location, from doc_classification.sujet. Replaces '- Par catégorie/'. Dry-run by default (returns the breakdown) ; pass apply=true to (re)build, or clear=true to remove the view (nothing else touched).",
    inputSchema: {
      apply: z.boolean().default(false).describe("(Re)build the symlink view."),
      clear: z.boolean().default(false).describe("Remove the - Sujets/ view."),
    },
  },
  async (args) => {
    const a = [];
    if (args.clear) { a.push("--clear"); return runAndFormat("sujet", "view", a); }
    if (args.apply) a.push("--apply");
    return runAndFormat("sujet", "view", a);
  }
);

server.registerTool(
  "connaissance_sujet_export",
  {
    description: "Materialize a subject on demand : COPY (or zip) all its documents into a real folder (e.g. to send to an accountant). Never touches the sources. Default dest: ~/Documents/- Sujets-export/<name>.",
    inputSchema: {
      name: z.string().describe("The subject name to export."),
      dest: z.string().optional().describe("Destination folder (default: - Sujets-export/<name>)."),
      zip: z.boolean().default(false).describe("Produce a .zip instead of a folder."),
    },
  },
  async (args) => {
    const a = [args.name];
    pushFlag(a, "dest", args.dest);
    if (args.zip) a.push("--zip");
    return runAndFormat("sujet", "export", a);
  }
);

// ── Duplicates (Phase D — doublons de ~/Documents) ─────────────

server.registerTool(
  "connaissance_duplicates_scan",
  {
    description: "Detect duplicates across the signal-extracted ~/Documents corpus : exact (same SHA256) + near (close text SimHash on the extractive summary, via doc_simhash). Reads from the doc_signals cache (already secret/container-free). Read content via SSD mirror. Read-only — returns clusters.",
    inputSchema: {},
    annotations: { readOnlyHint: true },
  },
  async () => runAndFormat("duplicates", "scan", [])
);

server.registerTool(
  "connaissance_duplicates_plan",
  {
    description: "Build a dedup manifest : keep one file per cluster (the best-filed) and mark the rest for the ledger trash. Writes nothing to the corpus — produces a reviewable plan→apply manifest.",
    inputSchema: {
      output_file: z.string().optional().describe("Write the full manifest here (else inline summary + transit path)."),
    },
  },
  async (args) => {
    const a = [];
    pushFlag(a, "output-file", args.output_file);
    return runAndFormat("duplicates", "plan", a);
  }
);

server.registerTool(
  "connaissance_duplicates_apply",
  {
    description: "Apply a dedup manifest : send each duplicate to the ledger trash (safe_trash — reversible with ledger revert, destroyed only by ledger purge). Dry-run by default ; pass apply=true to actually move.",
    inputSchema: {
      manifest: z.string().describe("Path to the duplicates manifest JSON."),
      apply: z.boolean().default(false).describe("Actually move to trash (default: dry-run)."),
    },
  },
  async (args) => {
    const a = [args.manifest];
    if (args.apply) a.push("--apply");
    return runAndFormat("duplicates", "apply", a);
  }
);

// ── Media (groupe B — médias par date) ─────────────────────────

server.registerTool(
  "connaissance_media_plan",
  {
    description: "Build a manifest to file MEDIA (images/audio/video) under ~/Documents/- Médias/AAAA/MM/ by date (date-in-name, else filesystem birth/mtime — no iCloud download). Code and exports are left as units by triage; this is the media-specific logic. Writes nothing to the corpus — produces a plan→apply manifest.",
    inputSchema: {
      scope: z.string().optional().describe("Restrict to a subfolder of ~/Documents."),
      output_file: z.string().optional().describe("Write the full manifest here."),
    },
  },
  async (args) => {
    const a = [];
    pushFlag(a, "scope", args.scope);
    pushFlag(a, "output-file", args.output_file);
    return runAndFormat("media", "plan", a);
  }
);

server.registerTool(
  "connaissance_media_apply",
  {
    description: "Apply a media manifest : move each media file to its dated folder VIA THE LEDGER (reversible with ledger revert). Dry-run by default ; pass apply=true to actually move. Name collisions handled ((2),(3)…).",
    inputSchema: {
      manifest: z.string().describe("Path to the media manifest JSON."),
      apply: z.boolean().default(false).describe("Actually move (default: dry-run)."),
    },
  },
  async (args) => {
    const a = [args.manifest];
    if (args.apply) a.push("--apply");
    return runAndFormat("media", "apply", a);
  }
);

// ── Entities (dédup du registre d'entités) ─────────────────────

server.registerTool(
  "connaissance_entities_candidates",
  {
    description: "Detect near-duplicate ENTITIES in the registry (e.g. ville-de-montreal vs ville-montreal, monteillet-conseil vs monteillet-conseil-inc, banque-nationale vs bnc). Lexical signals only (containment, token Jaccard, edit distance, acronym) across Synthèse fiches + entities in use in doc_classification. Read-only — a human picks the merge.",
    inputSchema: {},
    annotations: { readOnlyHint: true },
  },
  async () => runAndFormat("entities", "candidates", [])
);

server.registerTool(
  "connaissance_entities_merge",
  {
    description: "Merge one entity into another (from → into, format type/slug). Repoints doc_classification + files (atomic), appends the loser's name/aliases to the kept fiche's aliases, moves its summaries via the ledger and sends its fiche to the trash. Reversible with ledger revert. Dry-run by default ; pass apply=true to execute.",
    inputSchema: {
      from: z.string().describe("Entity to merge away (loser), format type/slug."),
      into: z.string().describe("Entity to keep (canonical), format type/slug."),
      apply: z.boolean().default(false).describe("Actually merge (default: dry-run)."),
    },
  },
  async (args) => {
    const a = ["--from", args.from, "--into", args.into];
    if (args.apply) a.push("--apply");
    return runAndFormat("entities", "merge", a);
  }
);

server.registerTool(
  "connaissance_entities_rename",
  {
    description: "Rename an entity's slug (same type) — e.g. re-accent (revenu-quebec → revenu-québec). Moves its folders (~/Documents, Synthèse, Résumés) via the ledger, updates the DB (entity_slug + rel_path segments + sujet values) and the fiche slug field. Reversible. Dry-run by default ; pass apply=true to execute.",
    inputSchema: {
      from: z.string().describe("Entity to rename, format type/old-slug."),
      to_slug: z.string().describe("New slug (same type), accents allowed."),
      apply: z.boolean().default(false).describe("Actually rename (default: dry-run)."),
    },
  },
  async (args) => {
    const a = ["--from", args.from, "--to-slug", args.to_slug];
    if (args.apply) a.push("--apply");
    return runAndFormat("entities", "rename", a);
  }
);

// ── Start stdio ────────────────────────────────────────────────

const transport = new StdioServerTransport();
await server.connect(transport);
