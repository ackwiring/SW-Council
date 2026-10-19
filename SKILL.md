---
name: sw-council
description: Run the parallel multi-model consensus software deliberation check on codebase changes directly from the command line. Use when the user asks to run a code audit, check security, verify database queries, or invokes /sw-council.
---

# Software Council (sw-council)

This skill reviews the current git diff with a council of models. It picks 3 or 4 of 8 lenses to fit
the diff (security, performance, developer experience, contrarian, first principles, expansionist,
outsider, executor). Each lens answers structured findings. Duplicates are merged, peers rank the
merged list when the lenses disagree, and a chairman rules: approve with conditions, or reject.

## Quick Start

Run the script from the repository you want reviewed, using this skill's folder (where this SKILL.md
lives, e.g. `~/.claude/skills/sw-council`):

```bash
python <skill-folder>/scripts/sw_deliberate.py                   # text report, ends COUNCIL_VERDICT: ...
python <skill-folder>/scripts/sw_deliberate.py --format json     # JSON ruling on stdout
```

Useful flags: `--base <ref>` reviews `<ref>...HEAD`, and `--rules <file>` / `--report <file>` give the
lenses the architecture rules and the deterministic report. `--lenses security,contrarian` picks the
lenses, and `--no-cache` forces a fresh review. Model ids are in `scripts/sw_council.config.json`. The
exit code is 0 for approved, 1 for rejected, and 2 when no ruling could be read. See README.md for the
JSON contract.

Report the ruling, the core justification, the action plan and the findings to the user. Never treat an
unreadable ruling (exit 2) as an approval.
