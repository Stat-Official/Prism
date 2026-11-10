"""Atomic JSON writes and physical-line JSONL reads."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def write_json(path: Path, data) -> None:
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')



def atomic_write(path: Path, content: str) -> None:
    """Readers see either the previous complete file or the next complete file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)



def read_jsonl(path: Path) -> list[dict]:
    # str.splitlines() splits U+2028/U+2029 inside valid JSON strings.
    # JSONL records are separated by physical newline bytes.
    with path.open(encoding='utf-8') as stream:
        return [json.loads(line) for line in stream if line.strip()]
