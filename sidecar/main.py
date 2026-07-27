"""PanelLens sidecar entry point.

Stdout is reserved for newline-delimited JSON IPC. Diagnostics must be written
to stderr or a log file.
"""

from __future__ import annotations

import json
import sys
from typing import Any


PROTOCOL_VERSION = 1


def respond(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(message: dict[str, Any]) -> dict[str, Any]:
    request_id = message.get("request_id")
    message_type = message.get("type")

    if message_type == "ping":
        return {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "status": "ok",
            "type": "pong",
        }

    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "status": "error",
        "error": {
            "code": "unsupported_message",
            "message": f"Unsupported message type: {message_type!r}",
        },
    }


def main() -> None:
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("The message must be a JSON object")
            respond(handle(message))
        except (json.JSONDecodeError, ValueError) as error:
            respond(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "request_id": None,
                    "status": "error",
                    "error": {
                        "code": "invalid_message",
                        "message": str(error),
                    },
                }
            )


if __name__ == "__main__":
    main()

