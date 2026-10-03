---
name: karpathy-guidelines
description: Behavioral guidelines that reduce common LLM mistakes in infrastructure and code work — surface assumptions, keep changes minimal and surgical, and define verifiable success criteria. Use when planning, writing, reviewing, or applying code, configuration, or system changes.
license: MIT
---

# Karpathy Guidelines — xCloud edition

Behavioral guidelines to reduce common LLM mistakes, derived from Andrej Karpathy's observations on LLM
coding pitfalls (original skill: github.com/multica-ai/andrej-karpathy-skills, MIT) and adapted for
agents that build and operate infrastructure. Worked examples: `EXAMPLES.md` in this directory.

**Tradeoff:** these guidelines bias toward caution over speed. On live systems, that is the right bias.

## 1. Think Before Acting

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before changing anything:
- Read the current state first (config, service status, versions, logs). Never change what you have not inspected.
- State your assumptions explicitly. If uncertain, ask.
- If the specification allows several readings, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum change that satisfies the specification. Nothing speculative.**

- No services, features, or options beyond what the documents specify.
- No abstractions for single-use code; no "flexibility" that wasn't requested.
- No error handling for impossible scenarios — but always handle the failure modes the design names.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code or configuration:
- Don't "improve" adjacent code, comments, formatting, or unrelated settings.
- Don't refactor things that aren't broken. Match the existing style.
- If you notice unrelated problems, record them in `DECISIONS.md` — don't fix them unasked.

When your changes create orphans:
- Remove files, units, packages, variables, or rules that YOUR changes made unused.
- Don't remove pre-existing state unless your task says so.

The test: every changed line, file, or setting traces directly to the task and a document section.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Turn tasks into verifiable goals:
- "Install X" → "X runs at the pinned version, its health check passes, its checklist items pass"
- "Open a port" → "the allowed source connects; a disallowed source is refused and logged"
- "Fix the bug" → "a test reproduces it, then passes"

For multi-step work, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Prefer checks that can fail: dry runs (`--check --diff`, `tofu plan`), validators, and negative tests.
Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** diffs contain only necessary changes, nothing is rebuilt because it
was over-engineered, and questions come before changes rather than after incidents.
