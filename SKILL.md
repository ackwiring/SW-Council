---
name: sw-council
description: Run the parallel multi-model consensus software deliberation check on codebase changes directly from the command line. Use when the user asks to run a code audit, check security, verify database queries, or invokes /sw-council.
---

# Software Council (sw-council)

This skill runs a parallel multi-model consensus deliberation engine against active workspace modifications (git diff), providing feedback through security, performance, developer experience, contrarian, first-principles, expansionist, outsider, and executor lenses, followed by peer-review and chairman synthesis.

## Quick Start

Run the script from the repository you want reviewed (it audits that repo's `git diff`), using the path of this skill's folder (where this SKILL.md lives), e.g. `~/.claude/skills/sw-council`:

```bash
python <skill-folder>/scripts/sw_deliberate.py
```
