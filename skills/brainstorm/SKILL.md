---
name: brainstorm
description: Use this skill when the user wants an intelligent multi-turn discussion or brainstorm on a topic. Claude actively participates — sharing what it agrees with, what it disagrees with, and introducing out-of-the-box angles — while Codex responds via the codex-ws-client WebSocket transport. The result is a real back-and-forth between two AI perspectives, not just a relay.
---

# Brainstorm

Orchestrate an intelligent multi-turn discussion between Claude and Codex on any topic. Claude does not relay — it participates: agreeing, pushing back, challenging assumptions, and introducing angles Codex may not have considered.

## Prerequisites

- `codex app-server` must be running at `ws://127.0.0.1:8765` (or specify `--uri`)
- `codex-ws-client` skill installed project-locally at `.codex/skills/codex-ws-client/` (per README install command), or globally at `~/.codex/skills/codex-ws-client/`

The commands below use the project-local path. Replace `.codex/` with `~/.codex/` if installed globally.

## How to run a brainstorm

### Step 0 — Ask how many rounds

Before starting, ask the user how many discussion rounds they want. Default is **3** if they don't specify or just want to get going.

### Step 1 — Open the topic

Send the opening prompt to Codex. Ask for its honest, direct take.

```bash
result=$(python .codex/skills/codex-ws-client/scripts/codex_ws_client.py --json "<topic>. Give me your honest, direct take.")
thread_id=$(echo "$result" | python -c "import json,sys; print(json.load(sys.stdin)['thread_id'])")
codex_response=$(echo "$result" | python -c "import json,sys; print(json.load(sys.stdin)['text'])")
```

### Step 2 — Claude forms its own view

Before crafting the next prompt, Claude must internally analyze Codex's response and identify:

- **Agreement**: what points are solid, well-reasoned, or surprising
- **Disagreement**: what is oversimplified, wrong, missing, or worth challenging
- **Novel angle**: what has not been considered — a contrarian view, a real-world constraint, an analogy from another domain, or a second-order consequence

### Step 3 — Claude responds to Codex

Craft a follow-up prompt that includes Claude's own position. Use this structure:

```
I agree with [X] — [brief reason why it's right or insightful].

I disagree with [Y] — [specific challenge or counter-evidence].

Here's an angle worth considering: [out-of-the-box idea, constraint, or framing Codex hasn't raised].

What's your take on [focused follow-up question]?
```

Send it on the same thread:

```bash
result=$(python .codex/skills/codex-ws-client/scripts/codex_ws_client.py --json --thread-id "$thread_id" "<claude's response above>")
codex_response=$(echo "$result" | python -c "import json,sys; print(json.load(sys.stdin)['text'])")
```

### Step 4 — Repeat for the agreed number of rounds

Continue: Codex responds → Claude analyzes → Claude pushes back or builds → send. Each round should go deeper, not broader. Drop topics that have been exhausted; press harder on the most interesting disagreements. Stop after the number of rounds agreed in Step 0.

### Step 5 — Draw conclusions

After the final discussion round, both sides independently form their conclusions.

**Claude's conclusion:** Claude writes its own conclusion first — the 2–3 strongest insights, the key remaining disagreement, and one concrete takeaway or recommendation.

**Codex's conclusion:** Then ask Codex for its conclusion on the same thread:

```bash
result=$(python .codex/skills/codex-ws-client/scripts/codex_ws_client.py --json --thread-id "$thread_id" "We've had a good discussion. Now give me your final conclusion: summarize the 2-3 strongest insights from our conversation, the key remaining disagreement or open question, and one concrete takeaway or recommendation.")
codex_conclusion=$(echo "$result" | python -c "import json,sys; print(json.load(sys.stdin)['text'])")
```

### Step 6 — Present conclusions side by side

Present both conclusions to the user in a clear side-by-side format:

```
## Conclusions

| Claude | Codex |
|--------|-------|
| <Claude's conclusion> | <Codex's conclusion> |
```

Use a two-column table or two clearly labeled sections so the user can easily compare perspectives and see where the two AIs converge and diverge.

## Tone and behavior

- Be direct. Don't hedge every disagreement with "that's a great point, but..."
- Challenge confidently. If Codex is wrong or shallow, say so and explain why.
- Bring outside perspectives. Draw on domains outside the immediate topic when relevant.
- Stay focused. Each round should sharpen one thread, not introduce five new ones.
- Don't just agree. If Codex says something obvious, call it out and push for depth.

## Example opening prompts

- "Are microservices still worth it in 2026, or has the pendulum swung too far back to monoliths?"
- "Is async/await a mistake that made codebases worse overall?"
- "Will AI agents replace junior developers within three years — honest take?"
- "Is Rust actually worth the learning curve for most teams?"
