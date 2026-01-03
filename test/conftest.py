import sys
import types
from pathlib import Path

# Mimic Dockerfile layout:
# In the container, the entire repo is copied to /app/backend
# and imports like `backend.core...` work because `/app` is on sys.path.
REPO_ROOT = Path(__file__).resolve().parents[1]

# 1) Ensure repo root is importable
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 2) Create a namespace package called "backend" that points at the repo root
# so imports like `backend.core.config` resolve to `<repo_root>/core/config.py`.
if "backend" not in sys.modules:
    backend_pkg = types.ModuleType("backend")
    backend_pkg.__path__ = [str(REPO_ROOT)]  # namespace package root
    sys.modules["backend"] = backend_pkg
