#!/usr/bin/env python3
"""Compatibility entrypoint: `python server.py` runs exactly what `python main.py` runs.

server.py used to be a 3,500 line monolith that duplicated config.py, process.py and api.py.
It had drifted (no /v1 gateway, no /api/vram/estimate, a different /api/active), so launchers
(systemd, launchd, Task Scheduler, Docker, README) now share one implementation through main.py.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import main  # noqa: E402

if __name__ == "__main__":
    main()
