# Basic Memory en serveur MCP local (stdio) pour Claude Code & Desktop

> **Statut (24 juin 2026, Basic Memory 0.22.1) — EN PLACE, approche local-only.**
> [Basic Memory](https://github.com/basicmachines-co/basic-memory) tourne en
> **stdio local**, branché dans **Claude Code** (scope user) et **Claude
> Desktop** sur le Mac. Aucun serveur HTTP, aucun port, aucune exposition réseau.
>
> **Pivot du 2026-06-24 : l'approche « exposé HTTPS via Tailscale pour l'app
> iPhone » a été ABANDONNÉE.** Raison (doc officielle Claude) : pour un
> connecteur MCP distant, *« the connection to your MCP server originates from
> Anthropic's servers, not from your machine's network interface »* et *« servers
> behind a VPN … won't connect »*. L'app iPhone est donc servie **par le cloud
> Anthropic**, qui ne voit pas un tailnet privé → `tailscale serve` (sans funnel)
> est structurellement invisible à l'app. Les objectifs « strict tailnet-privé »
> et « utilisable depuis l'app iPhone » sont **incompatibles**. Tout le volet
> Tailscale/HTTPS/serve/pmset/launchd est conservé en **annexe** pour mémoire.
>
> **Accès téléphone (2026-07-04) : pont vers la mémoire cloud CONFIRMÉ.** Push
> **manuel** curé (notes `#cloud`) depuis un chat Desktop → rappelé sur l'iPhone.
> Le push *automatique* (tâche planifiée) ne marche pas et a été retiré. Voir
> « Pont vers la mémoire cloud ».
>
> Détail des sections d'analyse ci-dessous (frontière, arbitrage des mémoires,
> collections qmd) : **toujours valides**, indépendantes du transport.

## Objectif

Un serveur de mémoire MCP qui stocke ses notes en **Markdown local-first**
(les `.md` ne vivent que sur le Mac), intégré au vault Obsidian existant, et
appelable depuis **Claude Code et Claude Desktop** (qui se connectent aux
serveurs MCP localement, depuis la machine). Accès mobile éventuel : **Obsidian
mobile** sur le vault, sans serveur ni exposition.

## Décisions de conception

- **Stockage** : `~/Connaissance/Mémoire/` — sous-dossier dédié dans le vault
  Obsidian existant (`~/Connaissance`), isolé des dossiers gérés par le pipeline
  (`Transcriptions/`, `Vues/`).
- **Intégration** : notes **indexées par qmd** (recherche unifiée) mais
  **exclues du scope du pipeline** `connaissance` — pas de
  classify/transcribe/audit dessus, pour éviter que `classify apply` déplace ou
  réécrive le frontmatter des notes de mémoire.
- **Sécurité** : entièrement déléguée à Tailscale (réseau privé au tailnet).
  Basic Memory en `streamable-http` n'a **pas d'auth intégrée**. Jamais de
  funnel, jamais d'exposition publique.

## Frontière & complémentarité avec `connaissance`

Les deux systèmes ne jouent pas le même rôle, et c'est ce qui les rend
complémentaires :

- `connaissance` possède les **entités du monde réel** (personnes, organismes,
  sujets) et tout savoir **dérivé d'une source ingérée** (documents, courriels,
  notes Apple). Dérivé, régénérable, réversible — la vérité est *le document*.
- Basic Memory possède le **savoir *authored* sans amont** : décisions de
  conception, contexte de projet, continuité conversationnelle. Originel, non
  régénérable — la vérité est *la note*.

**Test de routage** — « ce savoir trace-t-il vers un document/courriel/source
externe ? » Oui → `connaissance` (laisser le pipeline le produire). Non, il naît
d'une réflexion ou d'une conversation → Basic Memory.

**Pont autorisé, à sens unique.** Une note Basic Memory peut *référencer* une
entité `connaissance` (`[[Banque X]]`) ; l'inverse est **interdit** (les fiches
Synthèse ne pointent jamais vers la working memory, sinon le graphe dérivé
devient non-régénérable). Cela neutralise la **dérive de frontière** — le risque
d'avoir deux dossiers divergents pour la même entité.

**Trois barrières d'étanchéité :**

1. **Pipeline = aucune action requise** (vérifié dans le code le 2026-06-24).
   Le pipeline ne balaie **jamais** la racine `~/Connaissance` : `audit` cible
   des sous-arbres précis (`Transcriptions/`, `Résumés/`, `Synthèse/` ;
   `verifier_frontmatter` ne scanne que `[RESUMES, SYNTHESE]`), `scope` vise
   `~/Documents`, et `classify/organize` opèrent dans leurs arbres gérés.
   `Mémoire/` est donc **nativement invisible** au pipeline — pas de `scope
   exclude` (qui aurait ciblé `~/Documents`, le mauvais mécanisme).
2. **Collection qmd dédiée** — seul `qmd` balaie la racine (`**/*.md`) et
   happerait `Mémoire/`. Indexer `Mémoire/` comme collection qmd *séparée* de
   `connaissance` (⚠️ **amende** la décision initiale qui la fondait dans
   `connaissance`). On garde la recherche unifiée *à la demande* sans diluer le
   corpus documentaire avec des scratch-notes d'IA.
3. **Frontmatter disjoint** — ne jamais soumettre les notes Basic Memory à
   `CHAMPS_REQUIS`.

**Ce qui NE migre PAS vers Basic Memory** : Transcriptions, Résumés, Courriels,
Documents — et surtout **Synthèse** (fiches/chronologies/actions). La Synthèse a
la *forme* d'un graphe d'entités identique à Basic Memory, mais un *rôle* opposé :
dérivée, régénérable, couplée au pipeline (extraction d'actions, timelines,
`organize`). Migrer casserait la réversibilité et le modèle d'index dérivé.

## Arbitrage des trois mémoires (règle de routage)

Trois dépôts de mémoire coexistent désormais ; le recouvrement réel est entre
(1) et (2).

| | Mémoire fichier Claude `~/.claude/.../memory/` | Basic Memory `~/Connaissance/Mémoire/` | Pipeline `connaissance` |
|---|---|---|---|
| **Rappel auto** | Oui (`<system-reminder>`) | Non (le modèle doit interroger le MCP) | n/a |
| **Portée** | Claude Code, ce Mac uniquement | Téléphone + tout client MCP + Obsidian | Local |
| **Forme** | Atome court typé (`user/feedback/project/reference`) | Note riche, graphe, navigable | Dérivé du monde |
| **Vérité** | La note | La note | Le document source |

**Critère de tri unique :**

- « Claude doit-il l'avoir **injecté automatiquement** en début de session pour
  bien se comporter, même sans qu'on le demande ? » → **mémoire fichier Claude**
  (court, comportemental : `user`, `feedback`, pointeurs d'état `project`,
  `reference`).
- « Voudrais-je le **lire / lier / consulter moi-même**, éventuellement depuis le
  **téléphone** ? » → **Basic Memory** (substance : rationale de conception,
  narratif de projet, savoir conceptuel).

**Motif clé — pointeur + substance, jamais duplication :**

- La mémoire fichier Claude garde un **pointeur court** auto-rappelé :
  *« design dédup → Basic Memory `[[dedup-quasi-doublons-design]]` »*.
- La **substance** (le mémo complet) vit **une seule fois** dans Basic Memory,
  joignable depuis le téléphone. Une seule source de vérité par fait → aucune
  divergence.
- Pour rendre le pointeur **actionnable dans Claude Code**, connecter *aussi*
  Basic Memory à Claude Code en **stdio local** (`basic-memory mcp`, sans
  Tailscale) : le rappel auto surface le pointeur → Claude tire la substance de
  Basic Memory. Les deux systèmes se **composent** au lieu de se concurrencer.

**Application à l'existant** — migration *future*, **pas maintenant**, pas à pas
sur confirmation. Tri en trois sens :

- **Mémos de code/projet** (design ou état d'un codebase) → vivent dans le
  **repo de code** concerné (`docs/`, `CLAUDE.md`), ni dans Basic Memory ni dans
  la mémoire Claude. Concerne la **plupart** des mémos actuels, tous relatifs au
  système `connaissance` : `dedup-quasi-doublons-design`,
  `modele-sujets-virtuels`, `vues-snapshots-hors-icloud`,
  `caching-inefficace-en-batch`, `repasse-mistral-et-ocr-images`,
  `chantier-reorganisation-documents`.
- **Savoir personnel non-code** (hors codebase, ex. infra/setup perso comme
  `ssd-backup-cloud-et-acces-cowork`) → **Basic Memory**, migré un par un avec
  confirmation.
- **Atomes comportementaux** (`user`, `feedback`) + **pointeurs** → restent en
  mémoire fichier Claude (pour le rappel auto).

**Notes Apple** (règle de routage adjacente) : note-référence figée → pipeline
`note` ; note-*pensée* évolutive → naître dans Basic Memory.

## Architecture qmd : collections par couche

Aujourd'hui qmd a **une seule** collection `connaissance` sur tout
`~/Connaissance`. Problème : le **triplet** fait apparaître la même source
jusqu'à **3×** (transcription + résumé + synthèse) → quasi-doublons en
résultats. Cible : **découper par couche/altitude**, chaque requête tapant au
bon niveau.

| Collection | Contenu | Requête type |
|---|---|---|
| `transcriptions` | texte brut OCR/extrait | « le verbatim exact du doc Y » |
| `resumes` | résumés curés, 1/source | défaut : « que sais-je sur X » |
| `synthese` | fiches/chronologies par entité | « raconte-moi l'histoire de X » |
| `memoire` | notes Basic Memory *authored* | working memory / conceptuel |

Justification : la redondance la plus dure à séparer est transcription↔résumé
(brut vs curé du *même* contenu) — elle justifie à elle seule de sortir les
transcriptions. Résumés et synthèse se recouvrent moins (per-source vs agrégé) :
les garder distincts répond à deux questions différentes.

**Décision (2026-06-24) : implémentation différée, en une seule passe avec
l'install de Basic Memory.** Raison : la collection `memoire` exige que
`~/Connaissance/Mémoire/` existe (donc Basic Memory installé), et le découpage
oblige à retoucher les skills d'un **autre repo** — autant tout faire d'un coup,
de façon cohérente, plutôt qu'un additif provisoire.

Faits qmd (relevés le 2026-06-24, pour rendre la passe turnkey) :

- Config : **`~/.config/qmd/index.yml`** (YAML `collections: {<nom>: {path, pattern}}`).
  Aujourd'hui une seule collection `connaissance` → `~/Connaissance`, `**/*.md`.
- CLI : `qmd collection add/list/remove/rename/show` ; reindex via
  `qmd update` / `qmd embed` (MCP : `qmd-admin reindex`).
- **Embeddings locaux** (aucune clé API en config/env) → ré-indexer = temps CPU,
  **pas de coût $**.

Checklist de la passe (à exécuter avec Basic Memory) :

1. **Config** — remplacer la collection unique par 4 collections sur
   sous-dossiers :
   `transcriptions` → `~/Connaissance/Transcriptions` ·
   `resumes` → `~/Connaissance/Résumés` ·
   `synthese` → `~/Connaissance/Synthèse` ·
   `memoire` → `~/Connaissance/Mémoire` (toutes `**/*.md`). Vérifier au passage
   que qmd accepte plusieurs collections sur des sous-dossiers d'une même racine.
2. **Reindex + embed** (local).
3. **Retargeting des consommateurs** (repo `cowork-plugins/connaissance`, qui
   codent `["connaissance"]` en dur → cibler **`["resumes","synthese"]`** par
   défaut, transcriptions en opt-in) :
   - `skills/_shared/qmd-conventions.md` (la source de vérité « toujours
     `connaissance` » — à réécrire en premier)
   - `skills/organiser/SKILL.md`
   - `skills/synthetiser/SKILL.md` + `references/format-templates.md`
   - `skills/dashboard/SKILL.md` + `references/claude-md-template.md`
4. **Vérif** : une requête « que sais-je sur X » ne renvoie plus le même contenu
   en 2-3× (triplet dédupliqué).

**Interim léger** si 4 paraît lourd le moment venu : 3 collections —
`transcriptions` (brut) / curé (`resumes` + `synthese` fondus) / `memoire`.

## Mise en place (local-only, stdio) — FAIT le 2026-06-24

Pas de serveur HTTP, pas de Tailscale. Claude Code et Claude Desktop lancent
`basic-memory mcp` en **stdio** à la demande.

1. **Install** : `uv tool install basic-memory` (binaire
   `~/.local/bin/basic-memory`, v0.22.1).
2. **Projet** :
   ```bash
   mkdir -p ~/Connaissance/Mémoire
   basic-memory project add memoire ~/Connaissance/Mémoire
   basic-memory project default memoire
   ```
   (projet `main → ~/basic-memory` résiduel retirable : `project remove main`.)
3. **Claude Code** (scope user = tous les projets) :
   ```bash
   claude mcp add --scope user basic-memory -- /Users/guillaumemonteillet/.local/bin/basic-memory mcp
   claude mcp get basic-memory      # → Status: ✓ Connected, Type: stdio
   ```
   Écrit dans `~/.claude.json`. Retrait : `claude mcp remove basic-memory -s user`.
4. **Claude Desktop** : ajout dans `~/Library/Application Support/Claude/claude_desktop_config.json`,
   bloc `mcpServers` (à côté de `qmd`/`qmd-admin`), **chemin absolu** car le PATH
   des apps GUI n'inclut pas `~/.local/bin` :
   ```json
   "basic-memory": { "command": "/Users/guillaumemonteillet/.local/bin/basic-memory", "args": ["mcp"] }
   ```
   Prise en compte au **redémarrage de Claude Desktop**.

Points retenus : embeddings **fastembed local** (`bge-small-en-v1.5`, modèle
anglophone → recherche interne BM médiocre en FR ; qmd reste le moteur
principal). Frontière pipeline : rien à exclure (voir barrière 1).

## Pont vers la mémoire cloud de Claude (accès téléphone) — ✅ CONFIRMÉ 2026-07-04

L'app iPhone ne joint pas Basic Memory (cf. annexe), mais sa **mémoire chat** est
*account-scoped* et partagée web/Desktop/Mobile ([doc Claude](https://support.claude.com/en/articles/11817273-use-claude-s-chat-search-and-memory-to-build-on-previous-context)).
L'app **Desktop** voit Basic Memory (MCP local) **et** écrit cette mémoire → on
s'en sert de **relais**. **Testé de bout en bout** : un « mémorise ceci » dans un
chat Desktop est ensuite rappelé correctement **sur l'iPhone**. Il n'existe **pas**
de synchro fichier/API (mémoire auto-curée, opaque ; l'export/import est de la
migration, pas une synchro).

**Principe : push curé, MANUEL, à sens unique.** Basic Memory = source de vérité ;
la mémoire cloud = reflet pour le téléphone (lossy, résumé).

**⚠️ Le push AUTOMATIQUE ne marche pas.** Une tâche planifiée tourne dans la
surface cowork/agent, qui **n'a pas** la mémoire chat (feature claude.ai/app
uniquement) — son « Retiens durablement » n'est jamais persisté. La tâche
`memoire-cloud-push` a donc été **supprimée**. Le push doit se faire dans une
**vraie conversation de l'app Claude**.

**Réglages retenus :**
- **Scope = mémoire globale** (chat **hors-projet**), côté Mac ET iPhone. (Un
  Projet dédié isolerait du bruit mais impose de s'y placer des deux côtés.)
- **Push en une phrase depuis Desktop** : `basic-memory` est branché dans Claude
  Desktop (`claude_desktop_config.json`) → Claude lit les notes et mémorise dans
  la même conversation.
- **Curation par tag `#cloud`** : seules ces notes remontent. Le reste ne quitte
  jamais le Mac.

**Caveats :**
- Persistance **différée** (« prochaine mise à jour en arrière-plan ») : ne pas
  tester l'iPhone dans la foulée.
- La mémoire globale **se mélange** aux autres conversations et accumule des faits
  périmés → un push correctif écrase l'ancien (vérifié : la mémoire Tailscale
  périmée a bien été corrigée). Nettoyage occasionnel via **Réglages → Mémoire**.
- Ne JAMAIS rendre le pont bidirectionnel automatiquement (divergence).

### Gabarit push (à dire dans un chat **hors-projet** de Claude Desktop)

```text
Via le connecteur basic-memory (projet memoire), lis les notes taguées #cloud,
extrais-en les faits clés durables et utiles en mobilité, et MÉMORISE-les
durablement (mets à jour ta mémoire, corrige tout fait périmé). Basic Memory
reste la source de vérité : n'invente rien, ne contredis pas les notes. Sens
unique : ne réécris pas Basic Memory. Confirme ce que tu as retenu.
```

### Gabarit pull (manuel, chat Desktop)

```text
Qu'as-tu en mémoire concernant {SUJET} ? Pour chaque fait durable, écris ou
mets à jour une note dans Basic Memory (connecteur `basic-memory`, projet
`memoire`), en ajoutant le tag #cloud si pertinent. Ne duplique pas l'existant ;
corrige si divergent. Basic Memory est la source de vérité.
```

---

# Annexe — approche Tailscale/HTTPS (ABANDONNÉE le 2026-06-24)

Conservée pour mémoire. **Ne pas exécuter** : invalidée par le fait que l'app
Claude (web/iPhone) fait fetch le MCP depuis le **cloud Anthropic**, qui ne voit
pas un tailnet privé (cf. bandeau de statut en tête). Le seul moyen aurait été
`tailscale funnel` (public, exclu) + un proxy d'auth (Basic Memory n'a pas
d'auth). Décisions d'alors : veille inchangée, service reporté.

## Faits d'environnement (constatés le 24 juin 2026)

- `uv 0.10.11`, `tailscale` CLI présent, `python3 3.14.3`.
- Vault Obsidian = `~/Connaissance` (vault unique `f2a0eefb0551956e`).
- Tailnet actif, MagicDNS suffix `tail2b8c0a.ts.net` ; ce Mac =
  `macbookpro-de-guillaume.tail2b8c0a.ts.net` (100.88.161.117) ; iPhone
  `iphone173` présent dans le tailnet.
- Port **8000 libre** ; `tailscale serve` non configuré.
- pmset secteur : `tcpkeepalive` **déjà = 1** ; `sleep` = 1 (à passer à 0).
- ⚠️ **HTTPS non activé sur le tailnet** (`tailscale cert` →
  « HTTPS cert support is not enabled/configured »). **Prérequis bloquant.**

## Faits confirmés sur Basic Memory (doc à jour, à revérifier)

- Install : `uv tool install basic-memory`.
- Serveur HTTP distant :
  `basic-memory mcp --transport streamable-http --port 8000` → endpoint **`/mcp`**
  (ex. `http://localhost:8000/mcp`). Le transport `sse` existe aussi ;
  `streamable-http` est le bon pour un connecteur distant.
- Stockage = Markdown standard ; projets dans `~/.basic-memory/config.json` ;
  pointage d'un dossier via `basic-memory project add <nom> <chemin>`.

---

## Étape 0 — Prérequis manuel : activer HTTPS sur le tailnet

`tailscale serve` en HTTPS exige la fonctionnalité **HTTPS Certificates**, non
activable en CLI.

Console admin → **DNS** → activer **HTTPS Certificates**
(<https://login.tailscale.com/admin/dns>). Vérification après coup :

```bash
tailscale cert macbookpro-de-guillaume.tail2b8c0a.ts.net
# ne doit plus dire "not enabled" (l'émission/écriture d'un cert est normale)
```

Bloquant pour l'étape 4. Les étapes 1–3 et 6 peuvent se faire en parallèle.

## Étape 1 — Installer Basic Memory via uv

```bash
uv tool install basic-memory
basic-memory --version
```

## Étape 2 — Configurer le projet sur `~/Connaissance/Mémoire/`

```bash
mkdir -p ~/Connaissance/Mémoire
basic-memory project add memoire ~/Connaissance/Mémoire
basic-memory project default memoire
basic-memory project list                # vérifier le mapping
```

✅ **Fait le 2026-06-24** : projet `memoire` ajouté, mis par défaut, mode local
(MCP route `stdio`). Embeddings **fastembed local** (`bge-small-en-v1.5`,
aucun coût API). ⚠️ Modèle d'embedding *anglophone* → la recherche *interne* de
Basic Memory sur du contenu FR sera médiocre ; le moteur principal reste qmd
(étape collections). Un projet résiduel `main → ~/basic-memory` (créé à
l'install) peut être retiré : `basic-memory project remove main`.

Intégration `connaissance` :

- **Pipeline** : rien à faire — `Mémoire/` est nativement invisible (voir
  « Frontière & complémentarité », barrière 1). **Pas** de `scope exclude`.
- **qmd** : indexer `Mémoire/` comme **collection dédiée** (voir « Architecture
  qmd » — différé, en une passe avec le reste du découpage).

## Étape 3 — Lancer en mode serveur HTTP et confirmer le port

```bash
# --host 127.0.0.1 : n'écoute QUE sur localhost (défaut = 0.0.0.0 = toutes
# interfaces, ce qui exposerait en clair sur le LAN et l'IP Tailscale, court-
# circuitant le HTTPS de `serve`). Seule entrée = tailscale serve.
basic-memory mcp --transport streamable-http --host 127.0.0.1 --port 8000 --project memoire
# dans un autre shell :
lsof -nP -iTCP:8000 -sTCP:LISTEN
curl -s -i -X POST http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}'
```

✅ **Vérifié le 2026-06-24** : serveur en écoute sur `127.0.0.1:8000`, endpoint
`/mcp`, handshake `initialize` → `200 OK` + `serverInfo: "Basic Memory"`. Port
retenu : **8000**. `--path` défaut = `/mcp` (confirmé en 0.22.1).

## Étape 4 — Exposer en HTTPS interne au tailnet (après étape 0)

```bash
tailscale serve --bg 8000
tailscale serve status
```

Résultat attendu :

- URL HTTPS : `https://macbookpro-de-guillaume.tail2b8c0a.ts.net/`
- **Endpoint MCP pour le connecteur de l'app Claude :**
  `https://macbookpro-de-guillaume.tail2b8c0a.ts.net/mcp`

`serve` préserve le chemin (`/mcp` → `localhost:8000/mcp`). **Jamais `funnel`.**

## Étape 5 — Veille : garder le Mac joignable (sudo)

**Décision (2026-06-24) : ne PAS modifier la veille.** Le Mac dort normalement ;
l'iPhone ne le joindra que quand il est éveillé. (`tcpkeepalive` reste = 1.)
Option à la demande sans changement permanent : `caffeinate -s` quand on veut le
garder éveillé. Commande *non retenue* pour mémoire : `sudo pmset -c sleep 0`.

## Étape 6 — Persistance au démarrage (LaunchAgent)

**Décision (2026-06-24) : reportée — à trancher après l'étape 0** (HTTPS activé +
étape 4 validée), pour tout brancher d'un coup.

`tailscale serve --bg` est déjà persistant (état tailscaled). Reste à relancer
`basic-memory mcp` automatiquement via un LaunchAgent utilisateur
`~/Library/LaunchAgents/com.guioum.basic-memory.plist` :

- `ProgramArguments` = chemin absolu du binaire `basic-memory` +
  `mcp --transport streamable-http --port 8000`
- `RunAtLoad = true`, `KeepAlive = true`
- logs → `~/Library/Logs/basic-memory.{out,err}.log`
- chargement :
  `launchctl load -w ~/Library/LaunchAgents/com.guioum.basic-memory.plist`

Alternative légère : lancement manuel `nohup … &` à la demande.

---

## Vérification end-to-end (checklist de test)

1. **Serveur local** : `basic-memory mcp …` tourne ; `lsof -iTCP:8000` montre le
   LISTEN.
2. **Exposition tailnet** : `tailscale serve status` liste
   `https://macbookpro-de-guillaume.tail2b8c0a.ts.net` → `127.0.0.1:8000`.
3. **HTTPS joignable depuis le Mac** :
   `curl -sI https://macbookpro-de-guillaume.tail2b8c0a.ts.net/mcp` (TLS valide).
4. **Depuis l'iPhone** : app Claude → Réglages → Connecteurs → connecteur MCP
   custom avec `https://macbookpro-de-guillaume.tail2b8c0a.ts.net/mcp`. Si l'app
   exige OAuth → **plan B**.
5. **Écriture mémoire depuis le chat iPhone** : « retiens que X » → un `.md` est
   créé.
6. **Apparition dans Obsidian** : vault `~/Connaissance` → dossier `Mémoire/` →
   le `.md` est présent et lisible.
7. **Recherche qmd** : après reindex, le fait est retrouvable via une requête
   qmd (collection `connaissance`).

## Risques / plans B

- **App Claude iOS exige OAuth** : Basic Memory n'a pas d'auth en
  `streamable-http`. Plan B : proxy MCP gérant l'auth, ou tester d'abord depuis
  Claude Desktop/web pour isoler le problème. Le mode cloud OAuth de Basic Memory
  est à éviter (quitte le local-first).
- **Cert HTTPS** : sans l'étape 0, `tailscale serve` échoue sur le TLS — premier
  point à régler.
- **Accents dans le chemin** (`Mémoire`) : sans danger sur le FS macOS ;
  surveiller la normalisation Unicode si un outil s'en plaint (repli :
  `~/Connaissance/Memoire` sans accent).
