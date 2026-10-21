"""A fixture built from two real AOC-QC release runs on ackwiring/auto-format (2026-10-21):
run_01M5E01TBW9JJ7P5AJXT7509BX and run_01M5E0QS9C40AS1SM2VEQMZDCH. Both blocked on lens findings
that were invented or restated tool findings. Shared by test_grounding.py and test_cli.py."""

# The tracked .env.example as released: names and comments, every value empty.
ENV_EXAMPLE = """# Copy this file to .env and fill in real values. Never commit .env itself
# (it's gitignored) -- see the 2026-08-25 review for why that matters here.

# auto-format is a front-end over AOC-QC (ADR-0016 in ackwiring/AOC-QC). The gate, the brand
# checks, the Software Council release review and the packaging all run in AOC-QC, which calls the
# auto-format skill (~/local_git_projects/Skills/auto-format). Configure models and sw-council in
# AOC-QC, not here.

# Where AOC-QC is checked out and built (pnpm install && pnpm build). Default:
# ~/local_git_projects/AOC-QC. The server runs <AOCQC_HOME>/apps/cli/dist/main.js.
AOCQC_HOME=

# Where AOC-QC keeps each upload's workspace (aocqc check --upload). Default: ~/.aocqc/uploads.
AOCQC_UPLOADS_DIR=

# Path to the corporate Dockerfile template. Read by the auto-format skill's package step
# (prepareDockerWrapper), which aocqc package runs with this server's environment. If unset or
# missing, the skill logs that and uses its generic built-in template.
DOCKER_TEMPLATE_PATH=

# Shared-secret auth required on every /api/* request and the /api/logs
# WebSocket upgrade (server.js). Generate with:
#   node -e "console.log(require('crypto').randomBytes(32).toString('hex'))"
# Set BOTH lines to the SAME value -- REFORMER_API_KEY is checked by the
# backend, VITE_REFORMER_API_KEY is the same value baked into the frontend
# bundle (Vite's VITE_ prefix convention) so it can attach it to requests.
# This is a stopgap matching the backend staying reachable on the tailnet,
# not a real login system: anyone who can read the built frontend JS can
# read this key. Rotate by generating a new value and updating both lines.
REFORMER_API_KEY=
VITE_REFORMER_API_KEY=
"""


def _css() -> str:
    lines = [f".rule-{n} {{ margin: {n}px; }}" for n in range(1, 241)]
    lines[34] = "  border-right: 1px solid #1e293b;"
    lines[234] = "  background-color: #dcfce7;"
    return "\n".join(lines) + "\n"


INDEX_CSS = _css()


def _app() -> str:
    lines = [f"const v{n} = {n};" for n in range(1, 641)]
    lines[636] = "  fetch('/api/run').catch(() => {});"
    lines[637] = "  const style = { color: '#1e293b' };"
    return "\n".join(lines) + "\n"


APP_JSX = _app()

DELETED_PY_PATH = "Blasor_Agent/App/_gsdata_/_saved_/am_config_generator/generate_am_config.py"
DELETED_PY = "import re\nimport pandas as pd\nimport openpyxl\n" + "x = 1\n" * 440 + "value = eval(user_input)\n"


def new_file_diff(path: str, text: str) -> str:
    lines = text.splitlines()
    return (f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines))


def deleted_file_diff(path: str, text: str) -> str:
    lines = text.splitlines()
    return (f"diff --git a/{path} b/{path}\ndeleted file mode 100644\n--- a/{path}\n+++ /dev/null\n"
            f"@@ -1,{len(lines)} +0,0 @@\n" + "".join(f"-{line}\n" for line in lines))


RELEASE_DIFF = (new_file_diff(".env.example", ENV_EXAMPLE) + new_file_diff("src/index.css", INDEX_CSS)
                + new_file_diff("src/App.jsx", APP_JSX) + deleted_file_diff(DELETED_PY_PATH, DELETED_PY))

# The deterministic report: auto-format's brand findings, severity low and advisory; gitleaks clean.
REPORT = {
    "stages": [{"kind": "brand", "status": "failed", "reason": None},
               {"kind": "security_scans", "status": "passed", "reason": None}],
    "findings": [
        {"stage": "brand", "rule_id": "brand/off-palette-colour", "severity": "low", "file": "src/App.jsx",
         "line": 638, "message": "#1e293b is not a brand colour"},
        {"stage": "brand", "rule_id": "brand/off-palette-colour", "severity": "low", "file": "src/index.css",
         "line": 35, "message": "#1e293b is not a brand colour (4 uses in this file)"},
        {"stage": "brand", "rule_id": "brand/off-palette-colour", "severity": "low", "file": "src/index.css",
         "line": 235, "message": "#dcfce7 is not a brand colour"},
    ],
}

# What the lenses said in those runs (as raw lens JSON).
SECURITY_LENS = {"findings": [
    {"file": ".env.example", "line": None, "severity": "critical",
     "message": "Secrets should not be hard-coded. Use environment variables instead."},
    {"file": "src/index.css", "line": 35, "severity": "medium", "message": "Color #1e293b is not a brand colour"},
    {"file": "src/App.jsx", "line": 638, "severity": "medium", "message": "Color #1e293b is not a brand colour"},
    {"file": "src/index.css", "line": 235, "severity": "medium", "message": "Color #dcfce7 is not a brand colour"},
    {"file": ".env.example", "line": 17, "severity": "high",
     "message": "Secrets should be encrypted and never stored in plain text in version control."},
    {"file": ".env.example", "line": 18, "severity": "high",
     "message": "Secrets should not be exposed in public repositories or shared with untrusted parties."},
    {"file": ".env.example", "line": 31, "severity": "critical", "evidence": "REFORMER_API_KEY=",
     "message": "The API key is hard-coded in the repository."},
    {"file": ".env.example", "line": 16, "severity": "medium",
     "message": "Environment variable for storing the secret API key is missing a description and documentation."},
    {"file": DELETED_PY_PATH, "line": 444, "severity": "high",
     "message": "The script is using eval to evaluate user input, which can be extremely dangerous."},
    {"file": DELETED_PY_PATH, "line": 3, "severity": "critical",
     "message": "The script is using pandas and openpyxl, which can be susceptible to injection attacks."},
]}
PERFORMANCE_LENS = {"findings": [
    {"file": "src/App.jsx", "line": 638, "severity": "medium",
     "message": "Non-brand color usage in App.jsx may affect UI consistency and branding guidelines."},
    {"file": "src/index.css", "line": 35, "severity": "medium",
     "message": "Non-brand color usage in index.css may affect UI consistency and branding guidelines."},
    {"file": "server/temp/", "line": 1, "severity": "critical",
     "message": "The 'server/temp/' directory is not being cleaned up, potentially leading to memory leaks."},
    {"file": "server/uploads/", "line": 1, "severity": "critical",
     "message": "The 'server/uploads/' directory is not being managed properly."},
    {"file": DELETED_PY_PATH, "line": 1, "severity": "critical",
     "message": "The Python script 'generate_am_config.py' is deleted but still referenced."},
]}
