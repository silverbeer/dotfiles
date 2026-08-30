"""Import a skill script by path.

The scripts under test are not a package: board.py carries chezmoi's
executable_ prefix in the source tree, and apply.py reaches for linear_api by
path (LINEAR_CRUD_SCRIPTS, then its sibling skill, then $HOME). Both are loaded
from SKILLS_DIR (set by check-linear-crud.sh to a scratch copy of
dot_claude/skills): LINEAR_CRUD_SCRIPTS is pointed there, the scripts dir goes
on sys.path, and sys.modules['linear_api'] is pre-populated from that path so
`from linear_api import ...` resolves there and nowhere else.
"""

import importlib.util
import os
import pathlib
import sys

SKILLS_DIR = pathlib.Path(os.environ.get("SKILLS_DIR", "")).resolve()
LINEAR_CRUD_SCRIPTS = SKILLS_DIR / "linear-crud" / "scripts"


def load_module(name, skill, filename):
    if not SKILLS_DIR.is_dir():
        raise SystemExit(f"SKILLS_DIR is not a directory: {SKILLS_DIR!r} (run via check-linear-crud.sh)")
    path = SKILLS_DIR / skill / "scripts" / filename
    if not path.is_file():
        raise SystemExit(f"no such script: {path}")
    if str(LINEAR_CRUD_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(LINEAR_CRUD_SCRIPTS))
    os.environ["LINEAR_CRUD_SCRIPTS"] = str(LINEAR_CRUD_SCRIPTS)
    if "linear_api" not in sys.modules and name != "linear_api":
        _exec("linear_api", LINEAR_CRUD_SCRIPTS / "linear_api.py")
    return _exec(name, path)


def _exec(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod
