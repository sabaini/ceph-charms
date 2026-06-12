"""Make bundled Python dependencies importable by legacy action scripts."""

import sys
from pathlib import Path


CHARM_DIR = Path(__file__).resolve().parent.parent
VENV_LIB = CHARM_DIR / "venv" / "lib"

for site_packages in sorted(VENV_LIB.glob("python*/site-packages")):
    sys.path.insert(0, str(site_packages))
