import os
import sys
from pathlib import Path

SOURCE_ROOT = Path(
    os.environ.get(
        "PR_REMEDIATION_SOURCE_ROOT",
        Path(__file__).resolve().parents[3] / "cron" / "scripts",
    )
)
sys.path.insert(0, str(SOURCE_ROOT))
