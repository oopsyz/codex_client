---
name: brainstorm
description: Use this skill when the user wants an intelligent multi-turn discussion or brainstorm on a topic. Claude actively participates — sharing what it agrees with, what it disagrees with, and introducing out-of-the-box angles — while the peer model is driven either through the Codex WebSocket client or the Claude brainstorm adapter. The result is a real back-and-forth between two AI perspectives, not just a relay.
---

# Brainstorm

Orchestrate an intelligent multi-turn discussion between Claude and another agent on any topic. Claude does not relay — it participates: agreeing, pushing back, challenging assumptions, and introducing angles the peer may not have considered.

## Prerequisites

- For Codex as the peer: `codex app-server` must be running at `ws://127.0.0.1:8765` (or specify `--uri`) and `codex-ws-client` must be installed project-locally at `.codex/skills/codex-ws-client/` or globally at `~/.codex/skills/codex-ws-client/`
- For Claude as the peer from a Codex-driven workflow: `claude` CLI must be installed and authenticated, and `claude-cli-client` must be available under `skills/claude-cli-client/`

The commands below use project-local paths. Replace `.codex/` with `~/.codex/` if installed globally.

## Peer transports

- Claude driving Codex: use `.codex/skills/codex-ws-client/scripts/codex_ws_client.py`
- Codex driving Claude: use `skills/brainstorm/scripts/claude_brainstorm_client.py`

Both expose a compatible `--json` plus `thread_id` workflow for brainstorm orchestration.

## How to run a brainstorm

### Step 0 — Ask how many rounds

Before starting, ask the user how many discussion rounds they want. Default is **3** if they don't specify or just want to get going.

### Step 1 — Open the topic

Send the opening prompt to the peer model. Ask for its honest, direct take.

```bash
result=$(python .codex/skills/codex-ws-client/scripts/codex_ws_client.py --json "<topic>. Give me your honest, direct take.")
thread_id=$(echo "$result" | python -c "import json,sys; print(json.load(sys.stdin)['thread_id'])")
peer_response=$(echo "$result" | python -c "import json,sys; print(json.load(sys.stdin)['text'])")
```

If Claude is the peer instead of Codex, swap in:

```bash
result=$(python skills/brainstorm/scripts/claude_brainstorm_client.py --json "<topic>. Give me your honest, direct take.")
thread_id=$(echo "$result" | python -c "import json,sys; print(json.load(sys.stdin)['thread_id'])")
peer_response=$(echo "$result" | python -c "import json,sys; print(json.load(sys.stdin)['text'])")
```

### Step 2 — Claude forms its own view

Before crafting the next prompt, Claude must internally analyze the peer response and identify:

- **Agreement**: what points are solid, well-reasoned, or surprising
- **Disagreement**: what is oversimplified, wrong, missing, or worth challenging
- **Novel angle**: what has not been considered — a contrarian view, a real-world constraint, an analogy from another domain, or a second-order consequence

### Step 3 — Claude responds to the peer

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
peer_response=$(echo "$result" | python -c "import json,sys; print(json.load(sys.stdin)['text'])")
```

If Claude is the peer instead of Codex, swap in:

```bash
result=$(python skills/brainstorm/scripts/claude_brainstorm_client.py --json --thread-id "$thread_id" "<claude's response above>")
peer_response=$(echo "$result" | python -c "import json,sys; print(json.load(sys.stdin)['text'])")
```

### Step 4 — Repeat for the agreed number of rounds

Continue: the peer responds → Claude analyzes → Claude pushes back or builds → send. Each round should go deeper, not broader. Drop topics that have been exhausted; press harder on the most interesting disagreements. Stop after the number of rounds agreed in Step 0.

### Step 5 — Draw conclusions

After the final discussion round, both sides independently form their conclusions.

**Claude's conclusion:** Claude writes its own conclusion first — the 2–3 strongest insights, the key remaining disagreement, and one concrete takeaway or recommendation.

**Peer conclusion:** Then ask the peer for its conclusion on the same thread:

```bash
result=$(python .codex/skills/codex-ws-client/scripts/codex_ws_client.py --json --thread-id "$thread_id" "We've had a good discussion. Now give me your final conclusion: summarize the 2-3 strongest insights from our conversation, the key remaining disagreement or open question, and one concrete takeaway or recommendation.")
peer_conclusion=$(echo "$result" | python -c "import json,sys; print(json.load(sys.stdin)['text'])")
```

If Claude is the peer instead of Codex, swap in:

```bash
result=$(python skills/brainstorm/scripts/claude_brainstorm_client.py --json --thread-id "$thread_id" "We've had a good discussion. Now give me your final conclusion: summarize the 2-3 strongest insights from our conversation, the key remaining disagreement or open question, and one concrete takeaway or recommendation.")
peer_conclusion=$(echo "$result" | python -c "import json,sys; print(json.load(sys.stdin)['text'])")
```

### Step 6 — Present conclusions side by side

Present both conclusions to the user in a clear side-by-side format:

```
## Conclusions

| Claude | Peer |
|--------|-------|
| <Claude's conclusion> | <Peer conclusion> |
```

Use a two-column table or two clearly labeled sections so the user can easily compare perspectives and see where the two AIs converge and diverge. Label the peer explicitly as `Codex` or `Claude` in the final presentation.

## Tone and behavior

- Be direct. Don't hedge every disagreement with "that's a great point, but..."
- Challenge confidently. If the peer is wrong or shallow, say so and explain why.
- Bring outside perspectives. Draw on domains outside the immediate topic when relevant.
- Stay focused. Each round should sharpen one thread, not introduce five new ones.
- Don't just agree. If Codex says something obvious, call it out and push for depth.

## Example opening prompts

- "Are microservices still worth it in 2026, or has the pendulum swung too far back to monoliths?"
- "Is async/await a mistake that made codebases worse overall?"
- "Will AI agents replace junior developers within three years — honest take?"
- "Is Rust actually worth the learning curve for most teams?"
