from __future__ import annotations

import sys
from pathlib import Path

# Lab tooling intentionally lives outside the application package.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
