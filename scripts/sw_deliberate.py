#!/usr/bin/env python3
import os
import sys
import json
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
    "nemotron-mini:latest",
    "ministral-3:8b",
    "phi4-mini:latest"
]
CHAIRMAN_MODEL = "gemma4:latest"

# Try to load environment variables from .env file if it exists (for direct CLI execution)
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
        
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": parts}]
    }
    
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120.0) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            return res_data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        print(f"⚠️ Error querying Gemini model {model}: {e}")
        return ""

def query_model_sync(model: str, messages: list) -> str:
    # 1. Route Gemini requests directly to Gemini API if the key is available
    if GEMINI_API_KEY and "gemini" in model.lower():
        gemini_model = "gemini-2.5-flash" if "flash" in model.lower() else "gemini-2.5-pro"
        return query_gemini_sync(gemini_model, messages)
        
    # 2. Prefer real OpenRouter API key if set (and not pointing to local Ollama)
    if OPENROUTER_API_KEY and OPENROUTER_API_KEY != "ollama":
        return query_openrouter_sync(model, messages)
        
    # 3. If OPENROUTER_API_KEY is explicitly 'ollama' or has local URL, run it via the mapped Ollama completions endpoint
    if OPENROUTER_API_KEY == "ollama" or "localhost" in OPENROUTER_API_URL or "127.0.0.1" in OPENROUTER_API_URL:
        return query_openrouter_sync(model, messages)
        
    # 4. If GEMINI_API_KEY is set but no OpenRouter key is set, use Gemini as a general fallback
    if GEMINI_API_KEY:
        gemini_model = "gemini-2.5-flash" if "flash" in model.lower() else "gemini-2.5-pro"
        return query_gemini_sync(gemini_model, messages)
        
    # 5. Generic fallback to local Ollama API
    url = "http://localhost:11434/api/chat"
    payload = {
        "model": "qwen2.5:14b-instruct",
        "messages": messages,
        "stream": False
    }
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120.0) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            return res_data.get("message", {}).get("content", "")
    except Exception as e:
        raise RuntimeError(f"No API keys configured, and failed to connect to local Ollama: {e}")

async def query_model_async(executor, model: str, messages: list) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, query_model_sync, model, messages)

def parse_ranking_from_text(ranking_text: str) -> list:
    import re
    if "FINAL RANKING:" in ranking_text:
        parts = ranking_text.split("FINAL RANKING:")
        if len(parts) >= 2:
            ranking_section = parts[1]
            numbered_matches = re.findall(r'\d+\.\s*Response [A-Z]', ranking_section)
            if numbered_matches:
                return [re.search(r'Response [A-Z]', m).group() for m in numbered_matches]
            return re.findall(r'Response [A-Z]', ranking_section)
    return re.findall(r'Response [A-Z]', ranking_text)

def calculate_aggregate_rankings(stage2_results: list, label_to_model: dict) -> list:
    from collections import defaultdict
    model_positions = defaultdict(list)
    for result in stage2_results:
        parsed = parse_ranking_from_text(result['ranking'])
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

async def main():
    diff = get_git_diff()
    if not diff:
        print("⚠️ No active git modifications detected in the current workspace.")
        sys.exit(0)

    executor = ThreadPoolExecutor(max_workers=8)
    
    print("\n=======================================================", flush=True)
    print("           SOFTWARE COUNCIL CONSENSUS ENGINE", flush=True)
    print("=======================================================\n", flush=True)

    sem = asyncio.Semaphore(2)

    async def query_with_sem(model, messages, role_msg):
        async with sem:
            print(f"   ↳ [Active] {role_msg}: {model}...", flush=True)
            res = await query_model_async(executor, model, messages)
            print(f"   ✓ [Done] {role_msg}: {model}", flush=True)
            return res

    # --- STEP 1 ---
    print("🌀 Step 1: Stage 1 (Advisor Responses Collection) has started...", flush=True)
    
    stage1_tasks = []
    for model in COUNCIL_MODELS:
        messages = [
            {"role": "system", "content": "You are a software design auditor. Evaluate the git diff changes thoroughly."},
            {"role": "user", "content": f"Review this git diff and provide your feedback:\n\n{diff}"}
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
    
    ranking_prompt = f"""You are evaluating different code reviews of the following git diff:

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

    stage2_tasks = []
    for result in stage1_results:
        messages = [
            {"role": "system", "content": "You are a software peer reviewer. Compare and rank the other reviews fairly."},
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
    
    chairman_prompt = f"""You are the Software Council Chairman. Synthesize the following code reviews and peer rankings into a definitive, final code audit report and action plan.

User Git Diff:
{diff}

STAGE 1 Reviews:
{stage1_text}

STAGE 2 Peer Rankings:
{stage2_text}

Provide:
1. **Ruling**: Approve or Reject (needs another revision loop).
2. **Core Justification**: Key reasons for the ruling.
3. **Action Plan**: Detailed concrete action steps to address findings.
"""

    messages = [
        {"role": "system", "content": "You are the Software Council Chairman. Provide a clear, actionable synthesis."},
        {"role": "user", "content": chairman_prompt}
    ]
    
    final_report = await query_model_async(executor, CHAIRMAN_MODEL, messages)
    print("✅ Step 3 has been completed - chairman has synthesized the final consensus report and generated the action plan.", flush=True)
    
    print("\n=======================================================", flush=True)
    print("                 FINAL DELIBERATION REPORT", flush=True)
    print("=======================================================\n", flush=True)
    print(final_report, flush=True)
    print("\n=======================================================", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
