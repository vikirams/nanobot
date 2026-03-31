---
name: skill-creator
description: Create or update NanoBot AgentSkills. Use when designing, writing, or improving skills — including SKILL.md frontmatter, always-vs-ephemeral loading, slash command mapping, and sync between source and runtime skill directories.
metadata: '{"nanobot": {"always": false}}'
---

# NanoBot Skill Creator

Skills are markdown files that inject focused context into the LLM system prompt. Each skill should be concise, self-contained, and cover exactly one operation group.

---

## Skill File Format

Every skill is a directory containing `SKILL.md`:

```
skills/<skill-name>/
└── SKILL.md       ← required; frontmatter + instructions
```

**SKILL.md frontmatter** (required fields):

```yaml
---
name: skill-name
description: One-line description — what the skill does AND when to use it.
metadata: '{"nanobot": {"always": false}}'
---
```

- `name`: hyphen-case identifier matching the directory name
- `description`: the primary triggering mechanism — include both what it does and specific trigger contexts
- `metadata.nanobot.always`: `true` = injected into every session; `false` = injected on-demand

---

## Always vs. Ephemeral Loading

| Mode | `always` | When injected | Use for |
|------|----------|--------------|---------|
| Always-loaded | `true` | Every session | Persona, tone, universal rules (max 2–3 skills) |
| Ephemeral | `false` | When slash command fires or agent reads it | Focused operation skills |

Always-loaded skills add to **every** request's context — keep them lean (< 60 lines).

---

## Slash Command Mapping

Ephemeral skills are linked to slash commands in `nanobot/channels/webui.py` at `_PROMPT_SKILL_MAP`:

```python
_PROMPT_SKILL_MAP: dict[str, str] = {
    "discovery":       "hp-discovery",
    "segment":         "hp-segments",
    "enrich":          "hp-enrich",
    "merge":           "hp-dataops",
    "dedupe":          "hp-dataops",
    "push-to-segment": "hp-dataops",
}
```

To add a new slash-command skill: add an entry here AND create the skill directory.

---

## Writing Good Skills

**Description**: Include trigger phrases users would say, and what the skill handles. Be specific — vague descriptions lead to under-triggering.

**Body**:
- Write instructions in imperative form ("Pass `resultset_id`...", not "You should pass...")
- Include concrete code examples for any API calls — they prevent the agent from guessing
- Explain *why* rules matter, not just what to do
- Keep under 150 lines; if longer, move reference material to `references/` subdirectory

**Avoid**:
- Repeating rules already in other always-loaded skills
- Vague placeholders ("call the appropriate endpoint")
- Over-specifying things the LLM handles fine on its own

---

## Sync Protocol

Skills have two locations that must stay in sync:

| Location | Purpose |
|----------|---------|
| `nanobot/skills/<skill>/` | **Source** — git-tracked, authoritative |
| `~/.nanobot/workspace/skills/<skill>/` | **Runtime** — volume-mounted, read by agent |

**After any skill edit, sync source → runtime:**

```bash
for skill_dir in /Users/apple/Desktop/Workspace/nanobot/skills/*/; do
  cp -rf "$skill_dir" ~/.nanobot/workspace/skills/
done
# Also sync AGENTS.md
cp /Users/apple/Desktop/Workspace/nanobot/AGENTS.md ~/.nanobot/workspace/AGENTS.md
```

**Rule**: Source is authoritative. Never edit runtime files directly — changes will be lost on next sync.

---

## Adding a New Skill — Checklist

1. Create `nanobot/skills/<skill-name>/SKILL.md` with correct frontmatter
2. Write focused, concise body (code examples for any API patterns)
3. If triggered by slash command: add to `_PROMPT_SKILL_MAP` in `webui.py`
4. If always-loaded: set `"always": true` and keep body < 60 lines
5. Run the sync command above to push to runtime
6. Verify the skill appears in `~/.nanobot/workspace/skills/`
