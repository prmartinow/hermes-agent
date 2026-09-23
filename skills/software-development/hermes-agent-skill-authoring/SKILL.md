---
name: hermes-agent-skill-authoring
description: "Author and synthesize in-repo SKILL.md files and guides."
version: 2.2.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [skills, authoring, hermes-agent, conventions, skill-md]
    related_skills: [requesting-code-review]
    tags: [skills, authoring, synthesis, hermes-agent, conventions, skill-md]
    related_skills: [plan, sdlc-review]
---

# Authoring & Synthesizing Hermes-Agent Skills (in-repo)

## Overview

A Hermes skill encodes reusable procedural knowledge, operational standards, and battle-tested workflows so agents act predictably across sessions. Skills can live in two places:

1. **User-local:** `~/.hermes/skills/<maybe-category>/<name>/SKILL.md` (or profile directory) — personal and immediately active. Managed via `skill_manage(action='create')` or direct directory authoring.
2. **In-repo (primary focus of this guide):** `skills/<category>/<name>/SKILL.md` (bundled) or `optional-skills/<category>/<name>/SKILL.md` (opt-in) inside the repository. Shipped with Hermes, managed via `write_file` and git commits.

In-repo skills must meet strict **hardline authoring standards** (see `AGENTS.md`). Reviewers reject PRs with unvalidated frontmatter, bloated descriptions, or missing verification steps.

## When to Use

- Authoring a new reusable skill or updating an existing skill in the codebase.
- Synthesizing external community skills (from the Skills Hub, `skills.sh`, `ClawHub`, or GitHub) into a master in-repo skill.
- Refactoring complex multi-step workflows discovered during development into permanent procedural memory.
- **Don't use for:** purely temporary notes, project TODOs, or session-specific logs (use `session_search` or Hindsight episodic memory instead).

## The 6-Stage Skill Engineering & Synthesis Lifecycle

When creating or modernizing a skill—especially for complex domains like containerization, cloud infrastructure, or multi-agent orchestration—follow this structured 6-stage engineering lifecycle:

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ 1. DISCOVERY & SELECTION                                                               │
│    • Search Hermes Skills Hub (`hermes skills search <topic>`) & external registries.  │
│    • Select 4–10 top candidate skills representing diverse paradigms and approaches.   │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │
┌───────────────────────────────────────────▼────────────────────────────────────────────┐
│ 2. LOCAL STAGING CACHE (Isolated Analysis Scratchpad)                                  │
│    • Pull/inspect candidate skills into an isolated scratchpad cache:                  │
│      `~/.hermes/cache/skills-inspection/<topic>/` (keeps parent context lean).         │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │
┌───────────────────────────────────────────▼────────────────────────────────────────────┐
│ 3. MULTI-AGENT PARALLEL AUDIT (Fan-Out Pattern Extraction)                             │
│    • Dispatch parallel subagents via `delegate_task` to inspect each skill concurrently│
│    • Extract core architectures, execution recipes, templates, and anti-patterns.      │
│    • Consolidate findings into a disk ledger (`all_inspections_full.json`).           │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │
┌───────────────────────────────────────────▼────────────────────────────────────────────┐
│ 4. ENVIRONMENT & TASK GROUNDING                                                        │
│    • Audit host runtime: OS, available CLI tools, toolchains, daemons, and toolsets.  │
│    • Define concrete user tasks, execution constraints, and operational goals.         │
│    • Reconcile generic external patterns with local host and Hermes native realities.  │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │
┌───────────────────────────────────────────▼────────────────────────────────────────────┐
│ 5. SYNTHESIS & CANONICAL MATERIALIZATION                                               │
│    • Author the master skill directly into the canonical Hermes skill directory tree.  │
│    • Place master entrypoint in `SKILL.md` (hardline frontmatter, description ≤ 60 ch).│
│    • Modularize deep content into `references/`, `templates/`, and `scripts/`.         │
└───────────────────────────────────────────┬────────────────────────────────────────────┘
                                            │
┌───────────────────────────────────────────▼────────────────────────────────────────────┐
│ 6. FRONTMATTER VALIDATION, TESTING & DISCOVERY VERIFICATION                            │
│    • Run programmatic YAML and length validation.                                      │
│    • Author unit tests under `tests/skills/test_<skill>_skill.py`.                     │
│    • Verify that `skill_view` automatically recognizes `SKILL.md` & `linked_files`.    │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## Detailed Step-by-Step Procedure

### 1. Discovery & Selection
Search both local bundled skills and remote hubs for existing peer implementations:
```bash
# Search official and community skills via the Hermes CLI
hermes skills search <keyword>

# Inspect details before pulling
hermes skills inspect <identifier>
```
Identify 4–10 candidate implementations spanning different design choices.

### 2. Local Staging Cache (Scratchpad Isolation)
Do not dump dozens of raw multi-kilobyte skill files directly into your primary conversation context. Instead, stage them in an isolated inspection directory:
```bash
# Create an isolated inspection workspace
mkdir -p ~/.hermes/cache/skills-inspection/<topic>/
```
Pull or save candidate `SKILL.md` files into separate subdirectories (e.g. `~/.hermes/cache/skills-inspection/<topic>/<source>_<name>/SKILL.md`). This keeps your primary agent loop fast and free from prompt bloat.

### 3. Multi-Agent Parallel Audit
Use `delegate_task(tasks=[...])` to dispatch a parallel fleet of subagents. Each subagent audits a single staged skill against a structured extraction schema:
- **Core Capabilities & Architectural Patterns**: What specific mechanisms does this skill introduce?
- **Execution Recipes & Commands**: What exact tool calls or shell commands are invoked?
- **Invariants & Safety Rules**: What failure modes or anti-patterns does it guard against?
- **Prompt & Template Snippets**: What prompt headers or templates are provided?

Aggregate the JSON outputs into `~/.hermes/cache/delegation/all_inspections_full.json`.

### 4. Environment & Task Grounding
Before authoring the skill, ground the extracted patterns in the actual target environment and user objectives:
- **Host Audit**: Check `sys.platform`, installed tools (`docker --version`, `node -v`, `cargo --version`, `uv --version`), and background daemons.
- **Hermes Toolset Alignment**: Map shell commands to Hermes native tools (`search_files`, `read_file`, `patch`, `execute_code`, `delegate_task`, `terminal`).
- **Scope & Goal Boundary**: Define exactly what the new skill must do and what it intentionally leaves to other specialized skills.

---

### 5. Synthesis & Canonical Directory Materialization

The intermediate analysis lives in the cache scratchpad, but the **final synthesized master skill must be placed directly into the canonical Hermes skill directory structure** so that the Hermes runtime indexes, validates, and discovers it.

#### Canonical Placement Matrix

| Target Destination | Canonical Directory Path | When to Use |
| :--- | :--- | :--- |
| **User-Local (Active Profile)** | `~/.hermes/skills/<skill-name>/`<br>*(or `~/.hermes/profiles/<name>/skills/<skill-name>/`)* | Personal workflows, local custom tooling, profile-specific automation. Immediately active. |
| **In-Repo Bundled** | `skills/<category>/<skill-name>/` | Core, universally applicable skills shipped by default with Hermes (5+ sessions/month bar). |
| **In-Repo Optional** | `optional-skills/<category>/<skill-name>/` | Niche, vertical-specific, or heavy domain skills installed via `hermes skills install official/...`. |

#### Canonical Skill Directory Layout
Every synthesized skill should be organized into a clean, modular hierarchy:

```
<canonical-skill-root>/<skill-name>/
├── SKILL.md                          # Master entrypoint (frontmatter + core procedure, ~100-200 lines)
├── references/                       # Deep-dive guides, topologies, API specs (auto-discovered)
│   ├── deep-architecture.md
│   └── advanced-recipes.md
├── templates/                        # Starter configurations, boilerplate manifests (auto-discovered)
│   └── starter-config.yaml
└── scripts/                          # Deterministic Python / shell helpers (auto-discovered)
    └── helper_tool.py
```

---

### 6. Validation, Testing & Discovery Verification

1. **Validate Frontmatter Programmatically**:
   ```python
   import yaml, re, pathlib
   content = pathlib.Path("skills/<cat>/<name>/SKILL.md").read_text()
   assert content.startswith("---"), "Must start with --- at byte 0"
   m = re.search(r'\n---\s*\n', content[3:])
   fm = yaml.safe_load(content[3:m.start()+3])
   assert len(fm["description"]) <= 60, f"Description {len(fm['description'])} chars > 60"
   assert fm["description"].endswith("."), "Description must end with a period"
   assert "platforms" in fm, "platforms list required"
   ```
2. **Verify Hermes Discovery**:
   - Call `skill_view(name="<skill-name>")` to ensure Hermes successfully loads `SKILL.md` and populates the `linked_files` dictionary with all files under `references/`, `templates/`, and `scripts/`.
3. **Add Unit Test**: Create `tests/skills/test_<skill>_skill.py` using `pytest` and `unittest.mock`.
4. **Regenerate In-Repo Docs**: Run `python website/scripts/generate-skill-docs.py` (for in-repo skills) and discard unrelated diffs.

---

## Required Frontmatter Standards (HARDLINE)

```yaml
---
name: my-skill-name               # lowercase, hyphens, ≤64 chars
description: Concise capability statement, under sixty chars.
version: 0.1.0                    # semver; new skills start at 0.1.0
author: Real Name (github-handle), Hermes Agent
license: MIT
platforms: [linux, macos, windows]   # audit, don't guess
metadata:
  hermes:
    tags: [Short, Descriptive, Tags]
    related_skills: [other-in-repo-skill]
---
```

### Frontmatter Rules
- **`description` ≤ 60 characters**: Single sentence, ends with a period, no marketing fluff ("powerful", "seamless", "advanced"). Truncated at 57 chars in the system prompt index.
- **`author`**: Credit human contributors first: `Real Name (github-handle), Hermes Agent`.
- **`platforms`**: Audit actual imports/commands. Use `[linux, macos, windows]` for cross-platform tools; gate to `[linux, macos]` or `[linux]` only if POSIX/systemd-specific commands are mandatory.
- **`related_skills`**: Must resolve to existing in-repo skills in the active checkout.

---

## Standard Body Section Order

1. **Title & Summary**: `# <Skill> Skill` followed by 2–3 sentences stating purpose, capabilities, and boundaries.
2. **`## When to Use`**: Bulleted list of concrete user triggers and counter-triggers ("Don't use for").
3. **`## Prerequisites`**: Environment variables, required toolsets, and setup checks.
4. **`## Quick Reference`**: Fast command cheat-sheet or decision table.
5. **`## Procedure`**: Numbered, deterministic steps with checkable completion criteria.
6. **`## Pitfalls`**: Edge cases, known traps, and subtle error modes.
7. **`## Verification`**: Exact verification commands to prove success.

---

### `author` rules

- Credit the **human first**, then "Hermes Agent" as secondary collaborator: `Ben Barclay (benbarclay), Hermes Agent`.
- Never `author: Hermes Agent` alone for contributed skills — credit the human, not the tool, even (especially) when an agent drafted the text.
- Maintainer-authored skills: `Teknium (teknium1), Hermes Agent`.

### `related_skills` rules

- Every entry must resolve to an existing **in-repo** skill in the same tree state as your PR. Do not reference skills that were only planned, live in another PR, or exist only in `~/.hermes/skills/`.
- Verify each entry: `search_files(pattern='<name>', target='files', path='skills')` (and `optional-skills/`).

## Platform Gating: audit, don't trust

`platforms:` gates loading by host OS. Set it from what the skill's prose and scripts actually invoke:

| Skill uses only… | `platforms:` |
|---|---|
| Hermes tools + stdlib Python + cross-platform CLIs | `[linux, macos, windows]` |
| bash pipelines, `grep`/`awk`/`sed` chains, heredocs | `[linux, macos]` |
| `osascript`, `defaults`, `pmset` | `[macos]` |
| `apt`/`systemctl`/`/proc` | `[linux]` |

<!-- no-tmp: ok — names the anti-pattern skill authors must avoid -->
POSIX-only signals to search for in `scripts/`: `fcntl`, `termios`, `pty`, `os.fork`, `os.killpg`, `signal.SIGKILL`, `os.kill(pid, 0)` liveness checks, hardcoded `/tmp` `/proc` `/etc`. Default posture: fix cross-platform first (`tempfile.gettempdir()`, `pathlib.Path`, `psutil.pid_exists`); gate narrower only when the dependency is genuinely platform-bound, and say why in `## Pitfalls`.

## Size Limits

- Full SKILL.md: ≤ 100,000 chars enforced (`MAX_SKILL_CONTENT_CHARS`), but target **~100 lines for a simple skill, ~200 for a complex one**. Peer skills sit at 8-14k chars.
- Bulky or branch-specific material goes in `references/*.md`, `templates/`, or `scripts/` — pointed to from SKILL.md, not inlined.
- Don't expect the model to inline-write parsers or non-trivial logic every call — ship a helper script in `scripts/` and reference it by path.

## Body Structure (modern section order)

```
# <Skill> Skill
2-3 sentence intro: what it does, what it doesn't do, dependency stance.

## When to Use          — bulleted triggers (+ "Don't use for:" counter-triggers)
## Prerequisites        — exact env vars, installs, API key sourcing
## How to Run           — canonical invocation through the `terminal` tool
## Quick Reference      — flat command list, no narration
## Procedure            — numbered steps, each with a checkable completion criterion
## Pitfalls             — known limits, things that look broken but aren't
## Verification         — how to prove the skill worked
```

Not every section applies to every skill (a pure-procedure task skill may have no Quick Reference), but When to Use + actionable body + Pitfalls + Verification are the minimum. Cut marketing intros, "Setup Check" no-ops, and re-explanations of env vars already in Prerequisites.

### Reference Hermes tools, not raw shell

When the skill needs a capability, name the proper Hermes tool in backticks: `terminal`, `read_file`, `write_file`, `patch`, `search_files`, `web_search`, `web_extract`, `browser_navigate`, `vision_analyze`, `delegate_task`, `cronjob`. Do NOT name shell utilities the agent already has wrapped (`grep` → `search_files`, `cat` → `read_file`, `sed`/`awk` → `patch`, `find`/`ls` → `search_files target='files'`). A CLI-wrapper skill should frame invocations as `terminal(command="<tool> ...", timeout=...)` — bare shell prose ("run `foo --version`") is a review-blocking non-conformance. If the skill depends on an MCP server, name it and document setup in Prerequisites.

### Never use machine-local paths

Write repo-relative paths (`skills/...`, `tools/skill_manager_tool.py`). A `/home/<you>/...` path baked into a committed skill breaks for every other user and is an instant review flag.

## Writing Quality Principles

A skill exists to make the agent's process more predictable — the agent reliably follows the same useful discipline.

1. **Optimize for process predictability.** If a line does not change behavior, cut it.
2. **Choose the right context load.** The description is paid for every turn; details go in the body or linked references.
3. **End steps with completion criteria.** Checkable and, when it matters, exhaustive: "every modified file accounted for" beats "summarize changes."
4. **Co-locate rules with the concept they govern.**
5. **Use strong leading words** ("tight loop," "root cause," "regression test") over long repeated explanations.
6. **Prune duplication and no-ops.** "Be careful" and "use best practices" don't change model behavior — replace with a checkable criterion or delete.

## Tests and Docs (required for repo skills)

1. **Tests** live at `tests/skills/test_<skill>_skill.py` — stdlib + pytest + `unittest.mock` only, no live network. Run via `scripts/run_tests.sh tests/skills/test_<skill>_skill.py -q`. (The generic `tests/tools/test_skill_manager_tool.py` passing proves nothing about YOUR skill.)
2. **Docs regen:** run `python website/scripts/generate-skill-docs.py`, then apply scope discipline — the generator rewrites EVERY auto-gen page. `git checkout --` everything that isn't yours; the final diff must show only your SKILL.md, your one per-skill docs page, a one-line catalog row, and a one-line `website/sidebars.ts` insertion (verify with `search_files(pattern='<your-slug>', path='website/sidebars.ts')` — exactly one hit, or the page is an orphan).
3. **`.env.example`** (only if the skill needs new env vars): one clearly delimited commented block; touch nothing else in the file.

## Workflow

1. **Survey peers** in the target category with `search_files(target='files')` and read 2-3 peer SKILL.md files to match tone and structure. Prefer extending an existing skill over creating a narrow sibling.
2. **Decide tier and category** (see above). When in doubt, optional — and ask before pushing rather than defaulting.
3. **Draft** with `write_file` to `skills/<category>/<name>/SKILL.md` (or `optional-skills/...`).
4. **Validate locally**:
   ```python
   import yaml, re, pathlib
   content = pathlib.Path("skills/<category>/<name>/SKILL.md").read_text()
   assert content.startswith("---")
   m = re.search(r'\n---\s*\n', content[3:])
   fm = yaml.safe_load(content[3:m.start()+3])
   assert "name" in fm and "description" in fm
   assert len(fm["description"]) <= 60, f"description {len(fm['description'])} chars — hardline is 60"
   assert fm["description"].endswith(".")
   assert "platforms" in fm
   assert len(content) <= 100_000
   ```
   Also verify every `related_skills` entry exists in-repo.
5. **Add tests + regen docs** (previous section).
6. **Git add + commit** on the active branch; open a PR.
7. **Note:** the CURRENT session's skill loader is cached — `skill_view` / `skills_list` will not see the new skill until a new session. This is expected, not a bug.

## Editing Existing In-Repo Skills

- **Small fix:** `skill_manage(action='patch', ...)` works on in-repo skills, as does `patch`.
- **Major rewrite:** `write_file` the whole SKILL.md.
- **Supporting files:** `write_file` to `references/`, `templates/`, or `scripts/` under the skill dir.
- **Always commit** — in-repo skills are source, not runtime state. Re-run the docs generator when frontmatter changed.

---

## Common Pitfalls

| Anti-Pattern | Why It Fails | Correct Approach |
| :--- | :--- | :--- |
| **Description > 60 chars** | Truncates in model prompt index, diluting attention. | Keep strictly under 60 chars: `"Manage Docker containers, images, and Compose stacks."` |
| **Raw shell commands in prose** | Prompts model to run bash when native tools exist. | Point to native tools in backticks: `` `search_files` ``, `` `read_file` ``, `` `patch` ``. |
| **Monolithic 50 KB SKILL.md** | Overloads agent context window during skill load. | Move deep guides to `references/` and starter configs to `templates/`. |
| **Leaving final skill in cache** | Hermes skill loader won't discover or index it. | Materialize the final master skill into canonical `skills/`, `optional-skills/`, or `~/.hermes/skills/`. |
| **Un-grounded Hub Copying** | Fails when local host lacks specific cloud dependencies. | Ground patterns in local host toolchains and Hermes runtime before finalizing. |
| **Hardcoding machine paths** | Breaks on other user environments. | Use repository-relative paths or `get_hermes_home()`. |
