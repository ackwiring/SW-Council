#!/usr/bin/env python3
import os
import sys
import json
import re
import urllib.request
import urllib.error
import subprocess
import asyncio
from concurrent.futures import ThreadPoolExecutor

# Reconfigure stdout/stderr to use utf-8 to avoid encoding issues on Windows
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

# Local Ollama models configuration
COUNCIL_MODELS = [
    "qwen2.5-coder:latest",
    "glm4:latest",
    "gemma3:12b",
    "deepseek-r1:8b",
    "deepseek-coder:6.7b",
    "nemotron-3-nano:4b",
    "ministral-3:8b",
    "phi4-mini:latest"
]
CHAIRMAN_MODEL = "gemma4:12b"

# Only these names are read from a .env file. The search starts in the repo being
# audited, so a blanket load would copy that project's own secrets into this
# process (and the git subprocesses it spawns). OPENROUTER_API_URL is deliberately
# excluded: a .env in an audited repo could otherwise point it at any server and
# receive the OpenRouter Bearer key. Set it in the real environment instead.
DOTENV_ALLOWED_KEYS = {"OPENROUTER_API_KEY", "GEMINI_API_KEY"}

# Try to load API keys from a .env file if it exists (for direct CLI execution)
def load_dotenv():
    # Look in current directory and parent directories up to 4 levels
    current = os.path.abspath(os.getcwd())
    for _ in range(5):
        dotenv_path = os.path.join(current, ".env")
        if os.path.exists(dotenv_path):
            try:
                with open(dotenv_path, "r", encoding="utf-8") as f:
                    for line in f:
                        trimmed = line.strip()
                        if trimmed and not trimmed.startswith("#"):
                            parts = trimmed.split("=", 1)
                            if len(parts) == 2:
                                k, v = parts[0].strip(), parts[1].strip()
                                if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                                    v = v[1:-1]
                                # Skip non-key names, and never override a key already set
                                # in the shell (e.g. ~/.zshenv) with one from a repo's .env.
                                if k in DOTENV_ALLOWED_KEYS and not os.environ.get(k):
                                    os.environ[k] = v
            except Exception:
                pass
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

load_dotenv()

# Ensure we have active API keys
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OPENROUTER_API_URL = os.getenv("OPENROUTER_API_URL", "https://openrouter.ai/api/v1/chat/completions")

def get_git_diff():
    try:
        subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        diff = subprocess.check_output(["git", "diff", "HEAD"]).decode("utf-8")
        if not diff.strip():
            diff = subprocess.check_output(["git", "diff"]).decode("utf-8")
        return diff
    except Exception:
        return ""

def is_model_in_ollama(model: str) -> bool:
    try:
        req = urllib.request.Request("http://localhost:11434/api/tags")
        with urllib.request.urlopen(req, timeout=5.0) as response:
            data = json.loads(response.read().decode("utf-8"))
            local_models = []
            for m in data.get("models", []):
                name = m.get("name", "")
                local_models.append(name)
                if name.endswith(":latest"):
                    local_models.append(name[:-7])
            return model in local_models
    except Exception:
        return False

# Context sizing for local Ollama requests. A fixed num_ctx of 32768 overflowed
# as soon as a real app's diff (~30k tokens) was combined with the Stage 1
# reviews in Stage 2, and Ollama rejects an over-long request with HTTP 400.
# num_ctx is now sized per request: at least MIN_CTX, grown to fit the prompt,
# capped by the model's own trained context and by SW_COUNCIL_MAX_CTX (larger
# contexts may spill onto the CPU, which is slower but still completes).
MIN_CTX = 32768
MAX_CTX = int(os.getenv("SW_COUNCIL_MAX_CTX", "65536"))
REPLY_TOKENS = 4096
CHARS_PER_TOKEN = 3  # conservative for code-heavy prompts

_context_limits = {}

def model_context_limit(model: str) -> int:
    if model not in _context_limits:
        limit = MAX_CTX
        try:
            req = urllib.request.Request("http://localhost:11434/api/show",
                                         data=json.dumps({"model": model}).encode("utf-8"),
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10.0) as response:
                info = json.loads(response.read().decode("utf-8")).get("model_info", {})
            trained = [v for k, v in info.items() if k.endswith(".context_length")]
            if trained:
                limit = min(MAX_CTX, int(trained[0]))
        except Exception:
            pass
        _context_limits[model] = limit
    return _context_limits[model]

def estimate_tokens(messages: list) -> int:
    return sum(len(m["content"]) for m in messages) // CHARS_PER_TOKEN + 64 * len(messages)

def num_ctx_for(model: str, messages: list) -> int:
    needed = estimate_tokens(messages) + REPLY_TOKENS
    ctx = max(MIN_CTX, -(-needed // 8192) * 8192)
    return min(ctx, model_context_limit(model))

def fit_diff(diff: str, model: str, other_chars: int) -> str:
    """Trim the diff (never the reviews or instructions) so the prompt fits the model's context."""
    budget_tokens = model_context_limit(model) - REPLY_TOKENS - 256
    budget_chars = budget_tokens * CHARS_PER_TOKEN - other_chars
    if len(diff) <= budget_chars:
        return diff
    keep = max(budget_chars, 0)
    print(f"   ⚠️ Diff trimmed for {model}: kept {keep} of {len(diff)} chars to fit its {model_context_limit(model)}-token context.", flush=True)
    return diff[:keep] + f"\n\n[... diff truncated: {len(diff) - keep} more characters not shown ...]\n"

def ollama_chat(model: str, messages: list) -> str:
    url = "http://localhost:11434/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "options": {
            "num_ctx": num_ctx_for(model, messages)
        },
        "stream": False
    }
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=600.0) as response:
        res_data = json.loads(response.read().decode("utf-8"))
        return res_data.get("message", {}).get("content", "")

def query_ollama_sync(model: str, messages: list) -> str:
    try:
        return ollama_chat(model, messages)
    except Exception as e:
        print(f"⚠️ Error querying local Ollama model {model}: {e}")
        return ""

def query_openrouter_sync(model: str, messages: list) -> str:
    if not OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY is not set.")
    
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    
    target_model = model
    # If running locally against Ollama, map the model parameter to an installed local model
    if "localhost" in OPENROUTER_API_URL or "127.0.0.1" in OPENROUTER_API_URL or OPENROUTER_API_KEY == "ollama":
        model_lower = model.lower()
        if "gpt" in model_lower:
            target_model = "qwen2.5:14b-instruct"
        elif "grok" in model_lower:
            target_model = "ministral-3:8b"
        elif "gemini" in model_lower:
            target_model = "gemma3:12b"
        elif "claude" in model_lower:
            target_model = "qwen2.5-coder:latest"
        elif "llama" in model_lower:
            target_model = "deepseek-r1:8b"
        else:
            target_model = "qwen2.5:14b-instruct"
    else:
        # Map local model names to real OpenRouter model IDs when querying real OpenRouter API
        model_mapping = {
            "qwen2.5-coder:latest": "qwen/qwen-2.5-coder-14b-instruct",
            "qwen-2.5-coder-32b-instruct": "qwen/qwen-2.5-coder-14b-instruct",
            "glm4:latest": "zhipuai/glm-4-9b-chat",
            "gemma3:12b": "google/gemma-3-12b-it",
            "deepseek-r1:8b": "deepseek/deepseek-r1-distill-llama-8b",
            "deepseek-coder:6.7b": "deepseek/deepseek-coder-6.7b-instruct",
            "nemotron-3-nano:4b": "nvidia/nemotron-3-nano-30b-a3b",
            "ministral-3:8b": "mistralai/ministral-8b",
            "phi4-mini:latest": "microsoft/phi-4-mini-instruct",
            "gemma4:12b": "google/gemma-3-12b-it"
        }
        if model in model_mapping:
            target_model = model_mapping[model]

    payload = {
        "model": target_model,
        "messages": messages,
    }
    
    req = urllib.request.Request(OPENROUTER_API_URL, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120.0) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            return res_data['choices'][0]['message'].get('content', '')
    except Exception as e:
        print(f"⚠️ Error querying OpenRouter model {model}: {e}")
        return ""

def query_gemini_sync(model: str, messages: list) -> str:
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not set.")
    
    # Map messages to Gemini's content format
    parts = []
    for msg in messages:
        parts.append({"text": f"{msg['role'].upper()}: {msg['content']}"})
        
    # The key travels in the x-goog-api-key header, not a ?key= query string: URLs
    # are routinely written to proxy/server logs and exception tracebacks.
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [{"parts": parts}]
    }

    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120.0) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            return res_data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        print(f"⚠️ Error querying Gemini model {model}: {e}")
        return ""

def query_model_sync(model: str, messages: list) -> str:
    # 1. Prefer local Ollama if the model is installed locally
    if is_model_in_ollama(model):
        response = query_ollama_sync(model, messages)
        if response:
            return response

    # 2. Route Gemini requests directly to Gemini API if the key is available
    if GEMINI_API_KEY and "gemini" in model.lower():
        gemini_model = "gemini-2.5-flash" if "flash" in model.lower() else "gemini-2.5-pro"
        return query_gemini_sync(gemini_model, messages)
        
    # 3. Prefer real OpenRouter API key if set (and not pointing to local Ollama)
    if OPENROUTER_API_KEY and OPENROUTER_API_KEY != "ollama":
        return query_openrouter_sync(model, messages)
        
    # 4. If OPENROUTER_API_KEY is explicitly 'ollama' or has local URL, run it via the mapped Ollama completions endpoint
    if OPENROUTER_API_KEY == "ollama" or "localhost" in OPENROUTER_API_URL or "127.0.0.1" in OPENROUTER_API_URL:
        return query_openrouter_sync(model, messages)
        
    # 5. If GEMINI_API_KEY is set but no OpenRouter key is set, use Gemini as a general fallback
    if GEMINI_API_KEY:
        gemini_model = "gemini-2.5-flash" if "flash" in model.lower() else "gemini-2.5-pro"
        return query_gemini_sync(gemini_model, messages)
        
    # 6. Generic fallback to local Ollama API (skipped if step 1 already tried
    # this installed model -- retrying the identical request just fails again)
    if is_model_in_ollama(model):
        return ""
    return query_ollama_sync(model, messages)

async def query_model_async(executor, model: str, messages: list) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, query_model_sync, model, messages)

def parse_ranking_from_text(ranking_text: str, valid_labels=None) -> list:
    """Labels in ranked order from the FINAL RANKING section, each counted once.

    Positions used to come from every "Response X" mention, so repeats,
    made-up labels (Response L with only 8 reviews) and, when a model skipped
    the FINAL RANKING block, every mention in its whole evaluation all counted
    as ranks -- producing averages like 12.0 out of 8. A model that gives no
    FINAL RANKING section now contributes no ranking.
    """
    if "FINAL RANKING:" not in ranking_text:
        return []
    ranking_section = ranking_text.split("FINAL RANKING:")[-1]
    numbered = re.findall(r'^\s*\d+\.\s*\**\s*(Response [A-Z])\b', ranking_section, re.MULTILINE)
    labels = numbered or re.findall(r'Response [A-Z]\b', ranking_section)
    ranked = []
    for label in labels:
        if label in ranked or (valid_labels is not None and label not in valid_labels):
            continue
        ranked.append(label)
    return ranked

def calculate_aggregate_rankings(stage2_results: list, label_to_model: dict) -> list:
    from collections import defaultdict
    model_positions = defaultdict(list)
    for result in stage2_results:
        parsed = parse_ranking_from_text(result['ranking'], set(label_to_model))
        for position, label in enumerate(parsed, start=1):
            if label in label_to_model:
                model_positions[label_to_model[label]].append(position)
    
    aggregate = []
    for model, positions in model_positions.items():
        if positions:
            avg_rank = sum(positions) / len(positions)
            aggregate.append({
                "model": model,
                "average_rank": round(avg_rank, 2)
            })
    aggregate.sort(key=lambda x: x['average_rank'])
    return aggregate

def parse_verdict(report: str):
    """APPROVED, REJECTED, or None when the chairman's ruling can't be read."""
    patterns = [
        r"COUNCIL_VERDICT:\s*\**\s*(APPROVED|REJECTED)",
        r"STATUS:\s*\**\s*(APPROVED|REJECTED)",
        r"Ruling\W*\s*(Approve|Reject)",
    ]
    for pattern in patterns:
        matches = re.findall(pattern, report, re.IGNORECASE)
        if matches:
            return "APPROVED" if matches[-1].lower().startswith("approve") else "REJECTED"
    return None

async def main():
    diff = get_git_diff()
    if not diff:
        print("⚠️ No active git modifications detected in the current workspace.")
        sys.exit(0)

    executor = ThreadPoolExecutor(max_workers=8)
    
    print("\n=======================================================", flush=True)
    print("           SOFTWARE COUNCIL CONSENSUS ENGINE", flush=True)
    print("=======================================================\n", flush=True)

    # Use concurrency of 1 to prevent Ollama model VRAM thrashing on local hardware
    sem = asyncio.Semaphore(1)

    async def query_with_sem(model, messages, role_msg):
        async with sem:
            print(f"   ↳ [Active] {role_msg}: {model}...", flush=True)
            # One model failing must not abort the whole council (asyncio.gather
            # re-raises the first exception); drop it and carry on with the rest.
            try:
                res = await query_model_async(executor, model, messages)
            except Exception as e:
                print(f"   ⚠️ [Skipped] {role_msg}: {model} failed: {e}", flush=True)
                return ""
            if not res:
                print(f"   ⚠️ [Skipped] {role_msg}: {model} returned no response", flush=True)
                return ""
            print(f"   ✓ [Done] {role_msg}: {model}", flush=True)
            return res

    # --- STEP 1 ---
    print("🌀 Step 1: Stage 1 (Advisor Responses Collection) has started...", flush=True)
    
    stage1_tasks = []
    for model in COUNCIL_MODELS:
        stage1_system = "You are a software design auditor. Evaluate the git diff changes thoroughly."
        stage1_intro = "Review this git diff and provide your feedback:\n\n"
        model_diff = fit_diff(diff, model, len(stage1_system) + len(stage1_intro))
        messages = [
            {"role": "system", "content": stage1_system},
            {"role": "user", "content": f"{stage1_intro}{model_diff}"}
        ]
        stage1_tasks.append(query_with_sem(model, messages, "Querying advisor"))
        
    responses = await asyncio.gather(*stage1_tasks)
    
    stage1_results = []
    for model, resp in zip(COUNCIL_MODELS, responses):
        if resp:
            stage1_results.append({"model": model, "response": resp})
            
    if not stage1_results:
        print("❌ Step 1 failed: All models failed to respond.", flush=True)
        sys.exit(1)
        
    print(f"✅ Step 1 has been completed - successfully collected code audit feedback from {len(stage1_results)} separate model personas.", flush=True)

    # --- STEP 2 ---
    print("\n🌀 Step 2: Stage 2 (Peer Review and Ranking) has started...", flush=True)
    
    labels = [chr(65 + i) for i in range(len(stage1_results))]
    label_to_model = {f"Response {l}": res['model'] for l, res in zip(labels, stage1_results)}
    
    responses_text = "\n\n".join([f"Response {l}:\n{res['response']}" for l, res in zip(labels, stage1_results)])
    
    # Built per model, because the diff is trimmed to each model's context.
    ranking_template = """You are evaluating different code reviews of the following git diff:

--- GIT DIFF ---
{diff}
---

Here are the code reviews from different models (anonymized):

{responses_text}

Your task:
1. First, evaluate each response (Response A, B, C...) individually. Note its strengths and blind spots.
2. Provide a final ranking.

Your final ranking MUST be formatted EXACTLY as follows at the very end of your response:
FINAL RANKING:
1. Response C
2. Response A
3. Response B

Now provide your evaluation and ranking:"""

    stage2_system = "You are a software peer reviewer. Compare and rank the other reviews fairly."
    stage2_tasks = []
    for result in stage1_results:
        other_chars = len(stage2_system) + len(ranking_template) + len(responses_text)
        model_diff = fit_diff(diff, result['model'], other_chars)
        ranking_prompt = ranking_template.format(diff=model_diff, responses_text=responses_text)
        messages = [
            {"role": "system", "content": stage2_system},
            {"role": "user", "content": ranking_prompt}
        ]
        stage2_tasks.append(query_with_sem(result['model'], messages, "Peer reviewing"))
        
    rankings_resp = await asyncio.gather(*stage2_tasks)
    
    stage2_results = []
    for result, ranking in zip(stage1_results, rankings_resp):
        if ranking:
            stage2_results.append({
                "model": result['model'],
                "ranking": ranking
            })
            
    aggregate = calculate_aggregate_rankings(stage2_results, label_to_model)
    aggregate_str = ", ".join([f"{item['model']} (avg rank: {item['average_rank']})" for item in aggregate])
    print(f"✅ Step 2 has been completed - peer rankings collected and consensus computed: {aggregate_str}.", flush=True)

    # --- STEP 3 ---
    print("\n🌀 Step 3: Stage 3 (Chairman Synthesis) has started...", flush=True)
    
    stage1_text = "\n\n".join([f"Model: {res['model']}\nResponse: {res['response']}" for res in stage1_results])
    stage2_text = "\n\n".join([f"Model: {res['model']}\nRanking: {res['ranking']}" for res in stage2_results])
    
    chairman_system = "You are the Software Council Chairman. Provide a clear, actionable synthesis."
    chairman_diff = fit_diff(diff, CHAIRMAN_MODEL, len(chairman_system) + len(stage1_text) + len(stage2_text) + 1024)
    chairman_prompt = f"""You are the Software Council Chairman. Synthesize the following code reviews and peer rankings into a definitive, final code audit report and action plan.

User Git Diff:
{chairman_diff}

STAGE 1 Reviews:
{stage1_text}

STAGE 2 Peer Rankings:
{stage2_text}

Provide:
1. **Ruling**: Approve or Reject (needs another revision loop).
2. **Core Justification**: Key reasons for the ruling.
3. **Action Plan**: Detailed concrete action steps to address findings.

End your report with exactly one final line, either
COUNCIL_VERDICT: APPROVED
or
COUNCIL_VERDICT: REJECTED
"""

    messages = [
        {"role": "system", "content": chairman_system},
        {"role": "user", "content": chairman_prompt}
    ]

    final_report = await query_model_async(executor, CHAIRMAN_MODEL, messages)
    if not final_report:
        print(f"❌ Step 3 failed: chairman model {CHAIRMAN_MODEL} returned no synthesis.", flush=True)
        sys.exit(1)
    print("✅ Step 3 has been completed - chairman has synthesized the final consensus report and generated the action plan.", flush=True)
    
    print("\n=======================================================", flush=True)
    print("                 FINAL DELIBERATION REPORT", flush=True)
    print("=======================================================\n", flush=True)
    verdict = parse_verdict(final_report)
    # Callers (auto-format's UI) read the verdict from this one normalized line,
    # so the chairman's own copies are removed rather than printed twice.
    print(re.sub(r"(?im)^.*COUNCIL_VERDICT:.*$\n?", "", final_report).rstrip(), flush=True)
    print("\n=======================================================", flush=True)
    if verdict is None:
        print("COUNCIL_VERDICT: REJECTED", flush=True)
        print("❌ Could not parse a ruling from the chairman's report; failing closed.", flush=True)
        sys.exit(2)
    print(f"COUNCIL_VERDICT: {verdict}", flush=True)
    # Exit code is the gate: 0 only for APPROVED (and for "no changes" above).
    sys.exit(0 if verdict == "APPROVED" else 1)

if __name__ == "__main__":
    asyncio.run(main())
