"""Source-development Codex workspace. Excluded from the portable build."""
import os
from pathlib import Path

CODEX_WORKSPACE = Path(os.environ.get("CONTRACT_REVIEW_CODEX_WORKSPACE", str(Path(__file__).resolve().parent / "runtime" / "codex-workspace"))).expanduser().resolve()
