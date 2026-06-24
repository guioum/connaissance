# Basic Memory en serveur MCP, exposé HTTPS interne via Tailscale

> **Statut : plan documenté, non exécuté** (24 juin 2026). Ce guide décrit la
> mise en place prévue pour adosser un serveur de mémoire MCP
> [Basic Memory](https://github.com/basicmachines-co/basic-memory) au système
> `connaissance`, joignable depuis le chat de l'app Claude sur iPhone via une URL
> HTTPS **privée au tailnet** (jamais de `tailscale funnel`).
>
> Basic Memory évolue vite : **revérifier la commande d'install, le flag de
> transport HTTP, le port et le chemin d'endpoint sur le repo officiel avant
> d'exécuter**.

## Objectif

Un serveur de mémoire MCP qui stocke ses notes en **Markdown local-first**
(les `.md` ne quittent jamais le Mac), intégré au vault Obsidian existant, et
appelable depuis l'app Claude iOS via le tailnet.

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

1. **Scope pipeline** — `Mémoire/` exclu (`scope exclude`). Sinon
   `audit check --steps frontmatter_invalide` signale chaque note (Basic Memory
   écrit `type: note` sans `date`/`category`) et `classify/organize` peut la
   déplacer ou réécrire son frontmatter.
2. **Collection qmd dédiée** — indexer `Mémoire/` comme collection qmd
   *séparée* de `connaissance` (⚠️ **amende** la décision initiale qui la
   fondait dans `connaissance`). On garde la recherche unifiée *à la demande*
   sans diluer le corpus documentaire avec des scratch-notes d'IA.
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

Points à régler avant d'implémenter (config qmd, **pas maintenant**) :

- **Consommateurs qmd** : `organize enrich` (désambiguïsation d'entité) et le
  skill `synthetiser` (recherche) appellent qmd — décider quelle(s)
  collection(s) ils ciblent (probablement `resumes` + `synthese`).
- **Défaut de recherche manuelle** : union `resumes` + `synthese` ; les
  transcriptions en *opt-in* pour le verbatim.
- **Faisabilité config** : vérifier que qmd accepte plusieurs collections sur
  des sous-dossiers d'une même racine (`~/Connaissance/{Transcriptions,Résumés,
  Synthèse}` + `~/Connaissance/Mémoire`).
- **Coût** : 1 index + 1 cycle de reindex par collection (surcoût mineur).
- **Interim léger** si 4 paraît lourd : 3 collections — `transcriptions` (brut)
  / curé (`resumes` + `synthese` fondus) / `memoire`.

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
basic-memory project default memoire     # si la sous-commande existe (vérifier --help)
basic-memory project list                # vérifier le mapping
```

Intégration `connaissance` :

- **Exclure du pipeline** : `connaissance scope exclude ~/Connaissance/Mémoire`
  (outil MCP `connaissance_scope_exclude`) — scan/classify/transcribe/audit
  ignorent alors ce dossier.
- **qmd** : indexer `Mémoire/` comme **collection dédiée** (voir « Frontière &
  complémentarité » — ne pas le fondre dans la collection `connaissance`).
  Lancer un reindex (`qmd-admin reindex`) après les premiers écrits, à refaire
  quand le contenu évolue.

## Étape 3 — Lancer en mode serveur HTTP et confirmer le port

```bash
basic-memory mcp --transport streamable-http --port 8000
# dans un autre shell :
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/mcp
lsof -nP -iTCP:8000 -sTCP:LISTEN
```

Port retenu : **8000** (libre). Lancement au premier plan pour valider, puis
service (étape 6) ou background.

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

`tcpkeepalive` est déjà = 1 → rien à faire. Seule commande nécessaire :

```bash
sudo pmset -c sleep 0        # pas de veille système sur secteur
```

`displaysleep`/`disksleep` restent inchangés (écran/disque peuvent dormir, le
réseau reste actif).

## Étape 6 — Persistance au démarrage (LaunchAgent)

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
