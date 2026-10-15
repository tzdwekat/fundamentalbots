from pathlib import Path

import yaml

PERSONA_DIR = Path(__file__).parent / "personas"


def load_all() -> dict[str, dict]:
    out = {}
    for path in sorted(PERSONA_DIR.glob("*.yaml")):
        p = yaml.safe_load(path.read_text())
        out[p["name"]] = p
    return out
