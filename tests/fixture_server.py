#!/usr/bin/env python3
"""Run the test fixture playlist server standalone, bound to all interfaces.

Used to exercise a syncing container against a fake provider: the tests
proper bind to loopback, which a container cannot reach, so this variant
listens on 0.0.0.0 and the container is pointed at the host gateway.

    python tests/fixture_server.py [port]
"""

from __future__ import annotations

import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from integration import Handler  # noqa: E402


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"fixture server on http://0.0.0.0:{port}/playlist.m3u", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
