# headroom-pi-learn

> [`headroom learn`](https://github.com/headroomlabs-ai/headroom) plugin for the [pi coding agent](https://github.com/earendil-works/pi-mono) by earendil works.

Analyses pi session logs (`~/.pi/agent/sessions/`) for tool-call failure patterns and writes recommendations to `CLAUDE.md` — exactly like headroom's built-in Claude Code plugin, but for pi.

## What it does

`headroom learn` mines past sessions for recurring failures:

- Wrong file paths → adds correct paths to `CLAUDE.md`
- Missing modules / commands → documents them
- Repeated retries on the same error → prevents recurrence
- Verbosity calibration (`--verbosity`) → tunes output token reduction

## Install

```bash
# Install into headroom's environment
uv tool install "headroom-ai[proxy,ml]" --with boto3 --with botocore --with headroom-pi-learn
```

Or inject into an existing headroom install:

```bash
uv tool run --from headroom-ai pip install headroom-pi-learn
# or directly:
~/.local/share/uv/tools/headroom-ai/bin/pip install headroom-pi-learn
```

## Usage

```bash
# Dry-run: preview what would be written to CLAUDE.md / AGENT.md
headroom learn --agent pi --project /path/to/your/project

# Apply: write recommendations
headroom learn --agent pi --project /path/to/your/project --apply

# All projects at once
headroom learn --agent pi --all --apply

# Verbosity calibration — learn preferred output terseness from pi sessions
pi-verbosity             # dry-run: shows recommended level
pi-verbosity --apply     # writes ~/.headroom/verbosity.json + baseline
                         # hot-syncs to running headroom proxy
```

## How it works

Pi stores sessions as JSONL files in `~/.pi/agent/sessions/<encoded-project>/*.jsonl`.

Each session contains:
- `toolCall` blocks in assistant messages (`id`, `name`, `arguments`)
- `toolResult` messages matched by `toolCallId` (`toolName`, `content`)

The plugin parses these, classifies errors (file not found, permission denied, etc.) and feeds them into headroom's standard analysis pipeline — the same LLM-based pattern extractor used for Claude Code and Codex.

Recommendations are written into a `<!-- headroom:learn:start/end -->` block in `CLAUDE.md`, so they're automatically included in every pi session.

## Requirements

- Python ≥ 3.10
- `headroom-ai >= 0.27.0`
- pi sessions in `~/.pi/agent/sessions/`

## License

Apache 2.0
