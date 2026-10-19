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
    for path, chunk in files.items():
        block = f'<file-diff path="{path}">\n{chunk.rstrip()}\n</file-diff>'
        if len(block) > remaining:
            parts.append(f'<file-diff path="{path}" truncated="true">\n{chunk[: max(remaining - 80, 0)]}\n</file-diff>')
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
        out.append(
            {
                "file": str(item["file"]) if item.get("file") else None,
                "line": line if isinstance(line, int) and line > 0 else None,
                "lens": lens,
                "severity": severity if severity in SEVERITIES else "medium",
                "message": message[:2000],
                "suggested_fix": (str(item["suggested_fix"])[:4000] if item.get("suggested_fix") else None),
            }
        )
    return out


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
) -> dict:
    """The JSON output contract AOC-QC reads (ADR-0009, FR-42)."""
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
                "suggested_fix": f["suggested_fix"],
            }
            for f in merged
        ],
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
