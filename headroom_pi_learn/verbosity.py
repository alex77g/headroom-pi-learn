"""pi-verbosity — run headroom verbosity analysis on pi sessions.

Converts pi JSONL format to what headroom's verbosity scanner expects,
then calls extract_signals() + analyze() directly to build the baseline
and write verbosity.json.

Usage:
  python3 pi-verbosity        # dry-run
  python3 pi-verbosity --apply
"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SESSIONS_DIR = Path.home() / ".pi" / "agent" / "sessions"


def convert_session(src: Path, dst: Path) -> bool:
    """Convert pi JSONL → Claude Code JSONL. Returns True if non-empty."""
    lines: list[str] = []
    try:
        with open(src, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if entry.get("type") != "message":
                    continue

                msg  = entry.get("message", {})
                role = msg.get("role", "")
                ts   = entry.get("timestamp")
                cont = msg.get("content", [])

                if role == "assistant":
                    cc = []
                    for b in (cont if isinstance(cont, list) else []):
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "text":
                            cc.append({"type": "text", "text": b.get("text", "")})
                        elif b.get("type") == "toolCall":
                            cc.append({
                                "type": "tool_use",
                                "id": b.get("id", ""),
                                "name": b.get("name", ""),
                                "input": b.get("arguments", {}),
                            })
                    lines.append(json.dumps({
                        "type": "assistant", "timestamp": ts,
                        "message": {"role": "assistant", "content": cc, "usage": {}},
                    }))

                elif role == "user":
                    cc = []
                    for b in (cont if isinstance(cont, list) else []):
                        if isinstance(b, dict) and b.get("type") == "text":
                            cc.append({"type": "text", "text": b.get("text", "")})
                    if cc:
                        lines.append(json.dumps({
                            "type": "user", "timestamp": ts,
                            "message": {"role": "user", "content": cc},
                        }))

                elif role == "toolResult":
                    call_id = msg.get("toolCallId", "")
                    text = " ".join(
                        b.get("text", "") for b in (cont if isinstance(cont, list) else [])
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                    lines.append(json.dumps({
                        "type": "user", "timestamp": ts,
                        "message": {
                            "role": "user",
                            "content": [{"type": "tool_result", "tool_use_id": call_id, "content": text}],
                        },
                    }))
    except (OSError, UnicodeDecodeError):
        return False

    if not lines:
        return False
    dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    from headroom.learn.verbosity import analyze
    from headroom.proxy.output_savings import BaselineModel

    if not SESSIONS_DIR.exists():
        raise SystemExit(f"Pi sessions directory not found: {SESSIONS_DIR}")

    with tempfile.TemporaryDirectory(prefix="headroom-pi-verbosity-") as tmpdir:
        tmp = Path(tmpdir)
        paths: list[Path] = []

        for proj_dir in SESSIONS_DIR.iterdir():
            if not proj_dir.is_dir():
                continue
            for src in proj_dir.glob("*.jsonl"):
                dst = tmp / f"{proj_dir.name}__{src.name}"
                if convert_session(src, dst):
                    paths.append(dst)

        if not paths:
            raise SystemExit("No pi sessions found.")

        print(f"Analysing {len(paths)} sessions…")

        profile, baseline = analyze(paths, project_path=str(Path.cwd()))

        # Print results
        sig = profile.signals
        print()
        print(f"  Sessions : {sig.get('sessions')}")
        print(f"  Turns    : {sig.get('human_msgs')}  interrupts: {sig.get('interrupts')}")
        print(f"  Fast-skips: {sig.get('fast_skips')}/{sig.get('skip_eligible')}")
        print(f"  Echo ratio: {sig.get('mean_echo_ratio', 0):.1%}")
        print()
        print(f"  >> Verbosity level {profile.level}  ({profile.confidence} confidence)")
        print(f"     {profile.rationale}")
        print()

        if args.apply:
            headroom_dir = Path.home() / ".headroom"
            headroom_dir.mkdir(exist_ok=True)

            # Write verbosity.json
            profile.learned_at = datetime.now(timezone.utc).isoformat()
            vpath = headroom_dir / "verbosity.json"
            profile.save(vpath)
            print(f"  [WROTE] {vpath}")

            # Write output_savings.json baseline (headroom's native format)
            bpath = headroom_dir / "output_savings.json"
            bpath.write_text(json.dumps(baseline.to_dict(), indent=2))
            n_strata = len(baseline.strata)
            n_samples = baseline.glob.n if hasattr(baseline.glob, 'n') else 0
            print(f"  [WROTE] {bpath}  ({n_strata} strata, {n_samples} total samples)")

            # Hot-sync to running proxy
            import urllib.request, urllib.error
            try:
                req = urllib.request.Request(
                    "http://127.0.0.1:8787/admin/runtime-env",
                    data=json.dumps({
                        "HEADROOM_OUTPUT_SHAPER": "1",
                        "HEADROOM_VERBOSITY_LEVEL": str(profile.level),
                    }).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=2)
                print(f"  [SYNCED] proxy → HEADROOM_VERBOSITY_LEVEL={profile.level}")
            except (urllib.error.URLError, OSError):
                print(f"  (proxy not running — set HEADROOM_VERBOSITY_LEVEL={profile.level} manually)")
        else:
            print("  Dry run — use --apply to write verbosity.json and baseline.")


if __name__ == "__main__":
    main()
