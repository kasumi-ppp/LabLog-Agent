"""Keep pytest's temporary directories inside the project.

The sandbox denies writes to the system temp directory (WinError 5), so both
``tmp_path`` and any helper that uses ``tempfile`` must be pointed at a project
local directory before collection happens.
"""

import os
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SANDBOX = _ROOT / ".tmp" / "pytest"
_SANDBOX.mkdir(parents=True, exist_ok=True)

for _key in ("TMPDIR", "TEMP", "TMP"):
    os.environ[_key] = str(_SANDBOX)
tempfile.tempdir = str(_SANDBOX)
