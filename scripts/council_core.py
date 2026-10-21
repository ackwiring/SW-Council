"""Pure helpers for the Software Council: lens selection, structured findings, merging,
peer ranking, caching and model routing from config. Standard library only, no I/O except
the small cache class, so it can be tested without any model.

The five changes from the AOC-QC PRD live here:
1. pick 3-4 lenses per diff instead of all 8;
2. give the lenses context (rules file, deterministic report, touched files), not just the diff;
3. structured JSON findings, merged before peer review (no FINAL RANKING prose parsing);
4. lighter peer review: rank the merged findings list, skipped when the lenses already agree;
5. cache by diff + rules + lens + model, and model ids in config (fixes the Gemini 404 fallback).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field

SEVERITIES = ("info", "low", "medium", "high", "critical")
RULINGS = ("approve_with_conditions", "reject")


@dataclass(frozen=True)
class Lens:
    id: str
    name: str
    focus: str
    #: Paths or diff text that make this lens relevant (case-insensitive regex).
    triggers: str = ""
    #: Every diff gets some of this lens's attention (it can still be outranked).
    base: int = 0


LENSES: dict[str, Lens] = {
    lens.id: lens
    for lens in (
        Lens(
            "security",
            "Security Lead",
            "threat vectors, input validation, authentication and authorisation, secrets, injection, PII leaks",
            r"auth|login|logout|session|cookie|token|jwt|oauth|passw|secret|crypt|hash|sign|permission|role|acl|"
            r"sql|query|input|request|sanitiz|escape|upload|webhook|cors|csrf|xss|\.env",
        ),
        Lens(
            "performance",
            "Performance Lead",
            "runtime overhead, hot paths, N+1 queries, memory, blocking I/O, scale limits",
            r"for \(|while \(|\.map\(|\.forEach\(|select |join |index|cache|batch|stream|async|await|worker|"
            r"queue|loop|perf|benchmark|db/|database|migration",
        ),
        Lens(
            "dx",
            "Developer Experience Lead",
            "maintainability, clear names, small functions, testability, docs that match the code",
            r"test|spec|readme|docs?/|\.md$|refactor|rename",
            base=1,
        ),
        Lens(
            "contrarian",
            "Contrarian",
            "the likeliest way this breaks in production: edge cases, wrong assumptions, error paths",
            "",
            base=2,
        ),
        Lens(
            "first_principles",
            "First Principles Thinker",
            "whether the change solves the real problem, and the assumptions underneath it",
            r"^\+\+\+ b/.*(domain|core|model|schema|rules)",
        ),
        Lens(
            "expansionist",
            "Expansionist",
            "missed opportunities that would make the change scale or generalise",
            r"^new file mode",
        ),
        Lens(
            "outsider",
            "Outsider",
            "what a smart generalist with no context finds confusing or surprising",
            r"config|\.ya?ml$|\.toml$|\.json$|dockerfile|compose",
        ),
        Lens(
            "executor",
            "Executor",
            "the minimum shippable slice, release risk, rollback and what must happen before merge",
            r"changelog|version|release|deploy|package\.json|pyproject|\.lock$",
        ),
    )
}


def changed_files(diff: str) -> list[str]:
    """Paths touched by a unified diff, in order, without duplicates."""
    seen: list[str] = []
    for m in re.finditer(r"^diff --git a/(\S+) b/(\S+)$", diff, re.MULTILINE):
        path = m.group(2)
        if path not in seen:
            seen.append(path)
    return seen


def deleted_files(diff: str) -> set[str]:
    """Paths the diff deletes: they do not exist at the release commit."""
    return {path for path, chunk in split_diff(diff).items()
            if re.search(r"^deleted file mode|^\+\+\+ /dev/null", chunk, re.MULTILINE)}


def split_diff(diff: str) -> dict[str, str]:
    """The diff split into one chunk per file ("split by module")."""
    parts = re.split(r"(?=^diff --git )", diff, flags=re.MULTILINE)
    out: dict[str, str] = {}
    for part in parts:
        m = re.match(r"diff --git a/\S+ b/(\S+)", part)
        if m:
            out[m.group(1)] = part
    return out


def lens_scores(diff: str) -> dict[str, int]:
    """How strongly each lens applies to this diff: trigger hits in paths and added lines."""
    files = changed_files(diff)
    added = "\n".join(line for line in diff.splitlines() if line.startswith(("+", "new file mode")))
    text = "\n".join(files) + "\n" + added
    scores: dict[str, int] = {}
    for lens in LENSES.values():
        hits = len(re.findall(lens.triggers, text, re.IGNORECASE | re.MULTILINE)) if lens.triggers else 0
        scores[lens.id] = lens.base + min(hits, 20)
    if len(added.splitlines()) > 400:
        scores["first_principles"] += 3
    return scores


def select_lenses(
    diff: str,
    max_lenses: int = 4,
    requested: list[str] | None = None,
    dropped: tuple[str, ...] = (),
    min_lenses: int = 3,
) -> list[str]:
    """3-4 lenses for this diff (PRD change 1). `requested` overrides; `dropped` are lenses whose
    findings developers rarely accept. Ties keep the order in LENSES."""
    if requested:
        unknown = [lens for lens in requested if lens not in LENSES]
        if unknown:
            raise ValueError(f"unknown lens(es): {', '.join(unknown)}; known: {', '.join(LENSES)}")
        return list(dict.fromkeys(requested))
    scores = lens_scores(diff)
    order = list(LENSES)
    ranked = sorted(
        (lens for lens in order if lens not in dropped),
        key=lambda lens: (-scores[lens], order.index(lens)),
    )
    chosen = [lens for lens in ranked if scores[lens] > 0][:max_lenses]
    for lens in ranked:
        if len(chosen) >= min(min_lenses, max_lenses):
            break
        if lens not in chosen:
            chosen.append(lens)
    return chosen


def context_block(
    diff: str, rules_text: str | None, report: object | None, budget_chars: int
) -> str:
    """The lens input (PRD change 2): the rules file, the deterministic report, then the diff split
    by file. The diff is what gets cut when the budget is tight, never the rules or the report."""
    head: list[str] = []
    if rules_text:
        head.append(f"<rules>\n{rules_text.strip()}\n</rules>")
    if report is not None:
        head.append(
            "<deterministic-report>\n"
            + json.dumps(report, indent=1, sort_keys=True)[: budget_chars // 4]
            + "\n</deterministic-report>"
        )
    used = sum(len(h) for h in head)
    parts: list[str] = []
    remaining = max(budget_chars - used, 0)
    files = split_diff(diff) or ({"(diff)": diff} if diff else {})
    deleted = deleted_files(diff)
    for path, chunk in files.items():
        # A file deleted in the range is not part of the release: name it, do not show its old code.
        block = (f'<file-deleted path="{path}" note="deleted in this range; not in the release, do not review it"/>'
                 if path in deleted else f'<file-diff path="{path}">\n{chunk.rstrip()}\n</file-diff>')
        if len(block) > remaining:
            parts.append(block if path in deleted else
                         f'<file-diff path="{path}" truncated="true">\n{chunk[: max(remaining - 80, 0)]}\n</file-diff>')
            omitted = len(files) - len(parts)
            if omitted > 0:
                parts.append(f"[{omitted} more file(s) not shown to fit the context]")
            break
        parts.append(block)
        remaining -= len(block)
    return "\n\n".join(head + parts)


FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_json_object(text: str) -> dict | None:
    """The first JSON object in a model's answer: plain, fenced, or after some prose."""
    if not text:
        return None
    candidates = [m.group(1) for m in FENCE.finditer(text)] + [text]
    for candidate in candidates:
        start = candidate.find("{")
        while start != -1:
            depth, in_str, esc = 0, False, False
            for i in range(start, len(candidate)):
                ch = candidate[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            value = json.loads(candidate[start : i + 1])
                        except json.JSONDecodeError:
                            break
                        if isinstance(value, dict):
                            return value
                        break
            start = candidate.find("{", start + 1)
    return None


def normalise_findings(raw: object, lens: str) -> list[dict]:
    """A lens's findings, validated: unknown severities become medium, junk entries are dropped."""
    items = raw.get("findings") if isinstance(raw, dict) else None
    out: list[dict] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        message = str(item.get("message") or "").strip()
        if not message:
            continue
        line = item.get("line")
        severity = str(item.get("severity") or "medium").lower()
        evidence = item.get("evidence")
        out.append(
            {
                "file": normalise_path(str(item["file"])) if item.get("file") else None,
                "line": line if isinstance(line, int) and line > 0 else None,
                "lens": lens,
                "severity": severity if severity in SEVERITIES else "medium",
                "message": message[:2000],
                "suggested_fix": (str(item["suggested_fix"])[:4000] if item.get("suggested_fix") else None),
                "evidence": str(evidence)[:1000] if isinstance(evidence, str) and evidence.strip() else None,
            }
        )
    return out


def normalise_path(path: str) -> str:
    """A cited path as the diff spells it: forward slashes, no quotes or leading ./."""
    p = path.strip().strip("`'\"").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _words(message: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", message.lower()) if len(w) > 2}


def _same_issue(a: dict, b: dict) -> bool:
    """Same file, lines within 5 of each other (or both unknown), and mostly the same words."""
    if a["file"] != b["file"]:
        return False
    if (a["line"] is None) != (b["line"] is None):
        return False
    if a["line"] is not None and abs(a["line"] - b["line"]) > 5:
        return False
    wa, wb = _words(a["message"]), _words(b["message"])
    if not wa or not wb:
        return a["message"].strip().lower() == b["message"].strip().lower()
    return len(wa & wb) / len(wa | wb) >= 0.5


def merge_findings(findings: list[dict]) -> list[dict]:
    """Duplicates merged before peer review (PRD change 3): same file, nearby line and mostly the
    same words. The merged finding keeps the highest severity and lists every lens that saw it."""
    merged: list[dict] = []
    for f in findings:
        match = next((m for m in merged if _same_issue(m, f)), None)
        if match is None:
            merged.append({**f, "lenses": [f["lens"]]})
            continue
        if f["lens"] not in match["lenses"]:
            match["lenses"].append(f["lens"])
        if SEVERITIES.index(f["severity"]) > SEVERITIES.index(match["severity"]):
            match["severity"] = f["severity"]
        if not match["suggested_fix"] and f["suggested_fix"]:
            match["suggested_fix"] = f["suggested_fix"]
    for i, f in enumerate(merged, start=1):
        f["id"] = f"F{i}"
    return merged


# --------------------------------------------------------------------------- grounding
# A lens finding is kept only when it is grounded in the reviewed change and is not a deterministic
# tool finding said again. Small local models restate the tool report under their own lens and a
# higher severity, and invent issues (a "hard-coded secret" in a file whose values are all empty);
# both were seen in a real release run on 2026-10-21. Tool findings keep the tool's rule and
# severity: the council can refer to them, never re-raise or escalate them.

#: How far a cited line may be from a line the change shows (models miscount by a few lines).
LINE_SLACK = 3

_SECRET_CLAIM = re.compile(r"hard[\s_-]?coded|plain[\s_-]?text|committed|embedded|exposed|leak", re.IGNORECASE)
_SECRET_WORD = re.compile(r"secret|passw(or)?d|api[\s_-]?key|credential|private[\s_-]?key|token", re.IGNORECASE)
_ASSIGNMENT = re.compile(r"""^\s*(?:(?:export|const|let|var|final|static|private|public|readonly)\s+)*["']?[A-Za-z_][\w.\-]*["']?\s*[:=]\s*(.*)$""")
_ENV_REFERENCE = re.compile(
    r"^(\$\{?|%|process\.env|import\.meta\.env|os\.(environ|getenv)|getenv|env\(|config\.|settings\.)",
    re.IGNORECASE,
)
_PLACEHOLDER = re.compile(r"<[^>]*>|x+|\*+|\.\.\.|changeme|change[-_]me|your[-_ ].*|todo|none|null|undefined",
                          re.IGNORECASE)
_QUOTED_LITERAL = re.compile(r"""(["'`])([A-Za-z0-9_+/=.-]{8,})\1""")


def diff_new_lines(diff: str) -> dict[str, dict[int, str]]:
    """Per file, the new-side lines a unified diff shows (added and context), by line number."""
    out: dict[str, dict[int, str]] = {}
    for path, chunk in split_diff(diff).items():
        lines: dict[int, str] = {}
        number: int | None = None
        for raw in chunk.splitlines():
            hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            if hunk:
                number = int(hunk.group(1))
                continue
            if number is None or raw.startswith("\\"):
                continue
            if raw.startswith("-"):
                continue
            lines[number] = raw[1:] if raw[:1] in ("+", " ") else raw
            number += 1
        out[path] = lines
    return out


def _squash(text: str) -> str:
    return " ".join(text.split())


def quoted_lines(evidence: str, lines: dict[int, str]) -> list[int]:
    """The line numbers that hold the quoted evidence: every non-empty quoted line must appear
    (whitespace-insensitive), and the result is where the first one does."""
    parts = []
    for raw in evidence.splitlines():
        part = raw.strip()
        if part[:1] in ("+", "-") and not part.startswith(("++", "--")):
            part = part[1:].strip()
        part = _squash(part.strip("`"))
        if part:
            parts.append(part)
    if not parts:
        return []
    squashed = {n: _squash(text) for n, text in lines.items()}
    if not all(any(p in text for text in squashed.values()) for p in parts):
        return []
    return [n for n, text in squashed.items() if parts[0] in text]


def is_secret_claim(message: str) -> bool:
    """The finding says a secret is hard-coded, committed or exposed in the file."""
    return bool(_SECRET_CLAIM.search(message) and _SECRET_WORD.search(message))


def assigns_literal_value(text: str) -> bool:
    """The line puts a real value in the code: `KEY=value` with a non-empty, non-placeholder value
    that is not read from the environment, or a long quoted literal. Comments never count."""
    line = text.strip()
    if not line or line.startswith(("#", "//", "/*", "*", "--", ";")):
        return False
    m = _ASSIGNMENT.match(line)
    if m:
        value = re.split(r"\s+#", m.group(1), maxsplit=1)[0].strip().rstrip(",;").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'`":
            value = value[1:-1].strip()
        if value and not _ENV_REFERENCE.match(value) and not _PLACEHOLDER.fullmatch(value):
            return True
    # A token-like quoted literal (letters and digits, no spaces or calls), such as "AKIA...".
    return any(re.search(r"[A-Za-z]", m.group(2)) and re.search(r"\d", m.group(2))
               and not _PLACEHOLDER.fullmatch(m.group(2)) for m in _QUOTED_LITERAL.finditer(line))


_GENERIC_RULE_WORDS = {"rule", "rules", "tool", "check", "error", "warning", "issue", "found"}


def _topic_words(text: str) -> set[str]:
    words = {w.replace("color", "colour") for w in re.findall(r"[a-z0-9]+", text.lower())}
    return {w for w in words if len(w) > 3 and w not in _GENERIC_RULE_WORDS}


def _salient_tokens(text: str) -> set[str]:
    """Specific tokens two findings about the same thing share: hex colours, CVE/GHSA ids, versions."""
    tokens = set()
    for t in re.findall(r"[A-Za-z0-9][\w.@-]*", text or ""):
        t = t.lower().rstrip(".")
        if len(t) >= 4 and re.search(r"\d", t) and re.search(r"[a-z]", t):
            tokens.add(t)
    return tokens


def tool_findings(report: object) -> list[dict]:
    """The deterministic findings in a run report (AOC-QC's summary or a full aocqc.report/v1)."""
    items = report.get("findings") if isinstance(report, dict) else None
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("source", "tool") != "tool" or not item.get("file"):
            continue
        out.append({
            "rule_id": str(item.get("rule_id") or "tool"),
            "file": normalise_path(str(item["file"])),
            "line": item.get("line") if isinstance(item.get("line"), int) else None,
            "severity": str(item.get("severity") or ""),
            "message": str(item.get("message") or ""),
            "evidence": str(item.get("evidence") or ""),
        })
    return out


def duplicate_of(finding: dict, tools: list[dict]) -> dict | None:
    """The tool finding this lens finding restates, if any: same file, and either a specific token
    in common (a colour, a CVE id) or a nearby line with the tool rule's topic or wording."""
    if not finding.get("file"):
        return None
    text = f"{finding['message']} {finding.get('evidence') or ''}"
    tokens, words = _salient_tokens(text), _topic_words(text)
    for tool in tools:
        if tool["file"] != finding["file"]:
            continue
        if tokens & _salient_tokens(f"{tool['message']} {tool['evidence']}"):
            return tool
        near = finding["line"] is None or tool["line"] is None or abs(finding["line"] - tool["line"]) <= 2
        if not near:
            continue
        rule_words = _topic_words(re.sub(r"[/_.-]", " ", tool["rule_id"]))
        tool_words = _topic_words(tool["message"])
        overlap = len(words & tool_words) / len(words | tool_words) if words and tool_words else 0
        if words & rule_words or overlap >= 0.4:
            return tool
    return None


def _is_comment_or_blank(text: str) -> bool:
    line = text.strip()
    return not line or line.startswith(("#", "//", "/*", "*", "<!--", "--", ";"))


def grounding_problem(
    finding: dict, shown: dict[str, dict[int, str]], tools: list[dict], deleted: set[str] = frozenset()
) -> str | None:
    """Why a lens finding cannot stand, or None when it is grounded in the change."""
    dup = duplicate_of(finding, tools)
    if dup is not None:
        where = f"{dup['file']}:{dup['line']}" if dup["line"] else dup["file"]
        return (f"repeats the tool finding {dup['rule_id']} at {where}"
                + (f" ({dup['severity']})" if dup["severity"] else "")
                + "; tool findings keep the tool's rule and severity")
    path, line, evidence = finding.get("file"), finding.get("line"), finding.get("evidence")
    if not path:
        return "cites no file"
    if path in deleted:
        return f"{path} is deleted in the reviewed range, so it is not in the release"
    if path not in shown:
        return f"{path} is not in the reviewed change"
    lines = shown[path]
    if evidence:
        cited = quoted_lines(evidence, lines)
        if not cited:
            return f"the quoted evidence is not in {path} in the reviewed change"
        if line is not None:
            cited = [n for n in cited if abs(n - line) <= LINE_SLACK]
            if not cited:
                return f"the quoted evidence is not near {path}:{line}"
    elif line is not None:
        cited = [line] if line in lines else [n for n in lines if abs(n - line) <= LINE_SLACK]
        if not cited:
            return f"{path}:{line} is not a line the reviewed change shows"
        if line in lines and _is_comment_or_blank(lines[line]):
            return f"{path}:{line} is a comment or blank line, and the finding quotes nothing"
    else:
        return "cites no line or quoted text"
    if line is not None and line in lines and not evidence:
        cited = [line]  # a secret claim on a line is about that line, not its neighbours
    if is_secret_claim(finding["message"]) and not any(assigns_literal_value(lines[n]) for n in cited):
        return "claims a hard-coded secret, but no cited line assigns a value"
    return None


def ground_findings(findings: list[dict], diff: str, report: object | None) -> tuple[list[dict], list[dict]]:
    """Split lens findings into the grounded ones (kept, for peer review and the chairman) and the
    ones set aside, each with the reason. Set-aside findings never block and the chairman never
    sees them."""
    shown = diff_new_lines(diff)
    deleted = deleted_files(diff)
    tools = tool_findings(report)
    kept, set_aside = [], []
    for f in findings:
        problem = grounding_problem(f, shown, tools, deleted)
        if problem is None:
            kept.append(f)
        else:
            set_aside.append({**f, "reason": problem})
    return kept, set_aside


def uphold_ruling(ruling: dict, merged: list[dict]) -> tuple[dict | None, str | None]:
    """The chairman's ruling held to the grounded findings. Blocking ids must name findings the
    chairman was given. A reject that names none is a reject with no grounded finding: it becomes
    approve_with_conditions (with a note) when there are no grounded findings at all, and is
    unreadable (None: fail closed, exit 2) when grounded findings exist but none was named."""
    valid = {f["id"] for f in merged}
    named = [i for i in ruling["blocking_finding_ids"] if i in valid]
    dropped = [i for i in ruling["blocking_finding_ids"] if i not in valid]
    upheld = {**ruling, "blocking_finding_ids": named}
    if ruling["ruling"] != "reject" or named:
        note = f"ignored blocking ids that name no grounded finding: {', '.join(dropped)}" if dropped else None
        return upheld, note
    if not merged:
        note = ("the chairman rejected, but no grounded finding remained to stand on (lens findings that "
                "repeated tool findings or had no evidence were set aside), so the ruling is approve_with_conditions")
        return {**upheld, "ruling": "approve_with_conditions"}, note
    return None, "the chairman rejected without naming any grounded finding"


def lenses_agree(merged: list[dict], lens_count: int) -> bool:
    """Peer review adds nothing when the lenses already agree (PRD change 4): no findings at all,
    or every finding raised by at least two lenses (one lens alone always 'agrees')."""
    if lens_count <= 1 or not merged:
        return True
    return all(len(f["lenses"]) >= 2 for f in merged)


def parse_ranking(text: str, valid_ids: list[str]) -> list[str]:
    """A reviewer's ranking of finding ids, from {"ranking": [...]} or a plain numbered list."""
    obj = parse_json_object(text)
    found: list[str] = []
    raw = obj.get("ranking") if obj else None
    if isinstance(raw, list):
        found = [str(x).strip().upper() for x in raw]
    else:
        found = re.findall(r"\bF\d+\b", text.upper())
    out: list[str] = []
    for item in found:
        if item in valid_ids and item not in out:
            out.append(item)
    return out


def borda(rankings: list[list[str]], ids: list[str]) -> list[tuple[str, float]]:
    """Average position per finding id across reviewers; unranked ids sit after the last."""
    if not ids:
        return []
    totals = {i: 0.0 for i in ids}
    for ranking in rankings:
        for i in ids:
            totals[i] += ranking.index(i) + 1 if i in ranking else len(ids) + 1
    n = max(len(rankings), 1)
    return sorted(((i, round(totals[i] / n, 2)) for i in ids), key=lambda x: (x[1], ids.index(x[0])))


def parse_ruling(text: str) -> dict | None:
    """The chairman's JSON ruling, validated, or None. Accepts approve/reject spellings."""
    obj = parse_json_object(text)
    if not obj:
        return None
    ruling = str(obj.get("ruling") or "").strip().lower()
    if ruling.startswith("approve"):
        ruling = "approve_with_conditions"
    elif ruling.startswith("reject"):
        ruling = "reject"
    else:
        return None
    plan = obj.get("action_plan")
    blocking = obj.get("blocking_finding_ids")
    return {
        "ruling": ruling,
        "justification": str(obj.get("justification") or "").strip(),
        "action_plan": [str(x) for x in plan] if isinstance(plan, list) else [],
        "blocking_finding_ids": [str(x).upper() for x in blocking] if isinstance(blocking, list) else [],
    }


def output_record(
    ruling: dict,
    merged: list[dict],
    lenses: list[str],
    models: dict,
    stats: dict,
    set_aside: list[dict] | None = None,
    ruling_note: str | None = None,
) -> dict:
    """The JSON output contract AOC-QC reads (ADR-0009, FR-42). `findings` are the grounded ones;
    `set_aside` lists the lens findings dropped as tool repeats or ungrounded, never blocking."""
    blocking_ids = set(ruling["blocking_finding_ids"])
    return {
        "schema": "sw-council.ruling/v1",
        "ruling": ruling["ruling"],
        "justification": ruling["justification"],
        "action_plan": ruling["action_plan"],
        "findings": [
            {
                "id": f["id"],
                "file": f["file"],
                "line": f["line"],
                "lens": f["lenses"][0],
                "lenses": f["lenses"],
                "severity": f["severity"],
                # The chairman names what must be fixed before release; AOC-QC decides what blocks.
                "blocking": ruling["ruling"] == "reject" and f["id"] in blocking_ids,
                "message": f["message"],
                "evidence": f.get("evidence"),
                "suggested_fix": f["suggested_fix"],
            }
            for f in merged
        ],
        "set_aside": [
            {
                "file": f.get("file"),
                "line": f.get("line"),
                "lens": f["lens"],
                "severity": f["severity"],
                "message": f["message"],
                "reason": f["reason"],
            }
            for f in set_aside or []
        ],
        "ruling_note": ruling_note,
        "lenses": lenses,
        "models": models,
        "stats": stats,
    }


# --------------------------------------------------------------------------- cache (change 5)


def cache_key(diff: str, rules_text: str | None, lens: str, model: str) -> str:
    material = "\x00".join([diff, rules_text or "", lens, model])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass
class Cache:
    """Lens results on disk (SW_COUNCIL_CACHE_DIR, default ~/.cache/sw-council), keyed by diff
    hash + rules + lens + model; entries older than the TTL are ignored."""

    directory: str
    ttl_seconds: int = 14 * 24 * 3600
    enabled: bool = True
    hits: int = field(default=0)

    def get(self, key: str) -> list[dict] | None:
        if not self.enabled:
            return None
        path = os.path.join(self.directory, f"{key}.json")
        try:
            with open(path, encoding="utf-8") as handle:
                entry = json.load(handle)
        except (OSError, ValueError):
            return None
        if time.time() - entry.get("created", 0) > self.ttl_seconds or entry.get("key") != key:
            return None
        self.hits += 1
        return entry.get("findings")

    def put(self, key: str, findings: list[dict]) -> None:
        if not self.enabled:
            return
        os.makedirs(self.directory, exist_ok=True)
        tmp = os.path.join(self.directory, f"{key}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"key": key, "created": time.time(), "findings": findings}, handle)
        os.replace(tmp, os.path.join(self.directory, f"{key}.json"))


# --------------------------------------------------------------------------- config (change 5)

DEFAULT_CONFIG: dict = {
    "lens_models": ["gemma3:12b", "qwen2.5-coder:latest", "glm4:latest", "nemotron-3-nano:4b"],
    "chairman_model": "gemma4:12b",
    "max_lenses": 4,
    "peer_review": "auto",
    "dropped_lenses": [],
    "gemini_default_model": "gemini-2.5-flash",
}


def load_config(path: str | None) -> dict:
    """Model ids and council settings from a JSON file (`--config`, SW_COUNCIL_CONFIG or
    scripts/sw_council.config.json); missing keys keep their defaults."""
    config = dict(DEFAULT_CONFIG)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            raise ValueError(f"{path}: the config must be a JSON object")
        config.update({k: v for k, v in loaded.items() if not k.startswith("_")})
    if not isinstance(config["lens_models"], list) or not config["lens_models"]:
        raise ValueError("config: lens_models must be a non-empty list of model ids")
    if config["peer_review"] not in ("auto", "on", "off"):
        raise ValueError("config: peer_review must be auto, on or off")
    return config


PROVIDERS = ("ollama", "openrouter", "gemini", "fake")


def route(model: str) -> tuple[str | None, str]:
    """`provider:model` pins a provider and sends the id unchanged (no hard-coded remapping, which
    is what produced the Gemini 404s); a bare id keeps the old automatic routing (provider None)."""
    head, sep, rest = model.partition(":")
    if sep and head in PROVIDERS and rest:
        return head, rest
    return None, model


def assign_models(lenses: list[str], models: list[str]) -> dict[str, str]:
    """One model per lens, round-robin, so small fast models carry the lenses."""
    return {lens: models[i % len(models)] for i, lens in enumerate(lenses)}
