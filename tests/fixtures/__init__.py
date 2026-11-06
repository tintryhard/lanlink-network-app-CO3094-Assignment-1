"""Independent wire examples reusable by all team members."""
import json
from pathlib import Path


def load_protocol_cases() -> dict:
    return json.loads(Path(__file__).with_name("protocol_cases.json").read_text(encoding="utf-8"))
