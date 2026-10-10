# Household Expense Checker Agent

Student project for **Designing LLM/VLM Agents**.

## Setup

Create your own repository with **Use this template**, then clone your new
repository:

```bash
git clone <your-repository-url>
cd <your-repository-directory>
uv sync --locked
cp .env.example .env
uv run pytest
```

Configure your local model endpoint in `.env`. This project runs 100% locally on Apple Silicon (M1 Pro 16GB) without external keys or cloud dependencies. Start your local model server (e.g., Ollama running `Gemma 4 12B Q4_0` at `http://localhost:11434/v1`), configure `.env`, and verify the local connection:

```bash
uv run python -c 'from agent_project.llm import OpenRouterClient; print(OpenRouterClient().complete([{"role": "user", "content": "Reply with exactly COURSE_READY"}])["text"])'
```

## Coding Agent

If you already use Codex or Claude Code, nothing else is required. Otherwise,
install [Antigravity CLI](https://antigravity.google/docs/cli/install/) before
Meeting 1.

## Project Brief

- **Project title:** Household Expense Checker Agent
- **Intended user:** Individuals and households managing everyday personal budgets and shared domestic expenses (e.g., groceries, utility bills, subscriptions, dining, and family shopping).
- **Problem and primary task:** Maintaining a personal or household budget is tedious and frequently abandoned because bank statements often feature vague merchant names, mixed purchases (e.g., a supermarket trip combining groceries, home supplies, and electronics), and unexpected cost spikes (e.g., sudden utility rate increases or forgotten subscription renewals). The primary task is to inspect personal/household bank transactions, detect unusual or ambiguous expenses, proactively request brief clarification from the user before finalizing category assignments, and maintain an accurate household budget breakdown.
- **User requests (what they ask for):** "Review my latest household bank transactions, identify any unusual expenses or duplicates, ask me for clarification on ambiguous charges, and assign proper budget categories."
- **What a good result looks like:** Routine expenses (groceries, regular utilities, known subscriptions) are categorized accurately and silently; anomalous items (unusual spikes, unknown vendors, double billings) are explicitly flagged with concise clarification prompts; and all decisions are logged in a structured JSONL trace with latency and token usage.
- **Why an agent is appropriate:** Rigid budgeting rules or basic keyword matchers cannot disambiguate multi-category stores or detect contextual anomalies across a family's typical spending patterns. An LLM/VLM agent can reason about household spending habits, inspect smartphone photos of paper receipts to verify itemized purchases, ask contextual and low-friction clarification questions, and incorporate user feedback to refine categorization.
- **Planned data:** Anonymized household bank transaction logs (CSV/JSON containing timestamps, amounts, merchant descriptions, payment methods, and historical budget categories) and sample paper receipt photos (grocery slips, utility bills, restaurant receipts).
- **Planned tools:**
  - `get_household_transactions`: Fetch and filter recent personal/household bank transactions.
  - `compute_budget_baseline`: Calculate typical household spending ranges and anomaly thresholds by category and merchant (e.g., typical grocery basket size, normal utility bill ranges).
  - `request_user_clarification`: Prompt the household member with a concise question and suggested category choices for anomalous or ambiguous expenses.
  - `assign_category`: Record the confirmed budget category (e.g., Groceries, Utilities, Entertainment, Healthcare, Home Improvement) with a rationale and confidence rating.
- **Planned visual input:** Smartphone photos of supermarket receipts, restaurant slips, and utility bill statements to inspect itemized line items when transaction descriptions are vague or amounts deviate from typical household baselines.
- **Success criteria:**
  - Accurately identifies household spending anomalies (e.g., unusual price spikes, forgotten subscription renewals, duplicate charges).
  - Automatically categorizes at least 90% of routine household transactions without requiring user input.
  - Keeps clarification requests targeted, concise, and easy for the user to answer.
  - Operates reliably on local hardware (MacBook Pro M1 Pro 16GB) using quantized Gemma models (specifically `Gemma 4 12B Q4_0`) within reasonable execution bounds (<= 12 model steps per run).
- **Non-goals and safety boundaries:**
  - Non-goals: Direct bank account manipulation, automated bill payments, investment/tax advice, or debt management.
  - Safety boundaries: Zero handling or storage of real banking credentials, payment card numbers, or unredacted personal identifiers (PII). Operates only on sanitized personal finance data and avoids guessing ambiguous household expenses without user clarification.

## Starter Code

The repository contains only:

- `src/agent_project/llm.py` — an OpenRouter client and a mock client. A live
  call returns the text, tool calls, finish reason, model, provider, latency,
  and token usage with cost. Requests time out after 60 seconds and replies are
  limited to 1,024 tokens. If a provider returns an empty reply instead of the
  model's answer, the client retries once on another provider.
  `image_part(path)` adds an image to a message;
- `src/agent_project/tracing.py` — a JSON Lines trace helper;
- `tests/test_setup.py` — setup checks;
- `data/` — approved project data;
- `traces/` — generated traces, ignored by Git; and
- `evidence/` — the traces and results you submit.

The agent loop, domain tools, state, retrieval, multimodal behavior, evaluation,
and safety controls are intentionally left for you to design and implement.

## Model Execution & Hardware Target (MacBook Pro M1 Pro 16GB)

- **Local Model Target:** `Gemma 4 12B Q4_0` (4-bit quantized, e.g., via Ollama, llama.cpp, or MLX).
  - **Hardware compatibility:** On Apple Silicon M1 Pro with 16 GB unified memory, the 12B model at Q4_0 quantization requires ~6.8–7.5 GB of RAM (including KV cache for typical context lengths). This fits comfortably within the 16 GB envelope, leaving ~8–9 GB for macOS, system services, and local visual/multimodal preprocessing without risking disk swap.
  - **Why Gemma 4 12B Q4_0:** It represents the sweet spot for a 16GB M1 Pro machine: superior reasoning, instruction following, and tool-calling accuracy compared to smaller variants, while avoiding the memory pressure and swapping issues of larger 26B+ models.
  - **Multimodal Vision:** Built-in vision capabilities or paired document tools (e.g., `PaliGemma 2` / OCR preprocessing) for inspecting paper receipts, utility bills, and payment screenshots.
- **Run Constraints & Privacy:**
  - **100% Local & Free:** Zero cloud costs, zero external token limits, and complete data privacy for sensitive personal and household financial records.
  - **Step Limit:** Maximum 12 model steps per agent run to ensure responsiveness and tight decision loops.

## Working with Gemma Locally

- The model usually requests one tool call per turn. Inform it in the system
  prompt that independent calls may be requested together.
- For typed output, such as category assignments or parsed receipt data, offer
  one tool whose parameters match your Pydantic schema and validate its arguments.
  Keep `tool_choice` at its default (`"auto"`), and provide list maximum lengths;
  free-form JSON output can repeat itself until token exhaustion.
- Results vary between runs. Run your evaluation set at least three times locally
  and report both the average and worst-case performance.
- Use `MockClient` in automated unit tests (`pytest`) to ensure fast, deterministic CI.
- Copy traces submitted for coursework to `evidence/`. Check them first for keys,
  personal data, and absolute local paths.

## Commands

```bash
uv run pytest
uv run ruff check .
```

Use the assignments published on the course website as the source of truth.
