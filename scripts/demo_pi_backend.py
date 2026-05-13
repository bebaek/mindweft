from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from demo_peer_backend_common import request_json, run_peer_backend_demo  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    return run_peer_backend_demo(
        argv,
        expected_peer="pi",
        peer_label="Pi",
        warn_on_peer_mismatch=True,
        request_json_func=_request_json,
    )


def _request_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> Any:
    return request_json(method, url, payload, headers, timeout)


if __name__ == "__main__":
    raise SystemExit(main())
