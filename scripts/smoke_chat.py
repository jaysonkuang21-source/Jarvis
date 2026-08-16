"""Print the SSE event stream for one chat request.

Useful for checking the wire contract without the UI:
    uv run python scripts/smoke_chat.py "your question" --mode global
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("message", nargs="?", default="Summarise my notes.")
    parser.add_argument("--mode", choices=["local", "global"], default="local")
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--url", default="http://127.0.0.1:8756")
    args = parser.parse_args()

    payload = {
        "message": args.message,
        "profile": {
            "chat_model": args.model,
            "query_mode": args.mode,
            "community_level": 0,
        },
    }

    counts: dict[str, int] = {}
    with httpx.Client(timeout=120.0) as client:
        with client.stream("POST", f"{args.url}/api/chat", json=payload) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                kind = event["type"]
                counts[kind] = counts.get(kind, 0) + 1
                if kind == "token":
                    sys.stdout.write(event["text"])
                    sys.stdout.flush()
                elif kind == "retrieval_progress":
                    print(f"  .. {event['current']}/{event['total']} {event['label']}")
                else:
                    print(f"[{kind}] {json.dumps(event, default=str)[:160]}")

    print("\n\nevent counts:", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
