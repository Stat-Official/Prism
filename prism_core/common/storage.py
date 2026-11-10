"""Collection selection, pipeline logs and reusable body results."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from prism_core.common.config import KST
from prism_core.common.files import write_json
from prism_core.common.paths import BASE


def read_pipeline_log(folder: Path) -> dict:
    path = folder / 'pipeline_log.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}



def update_pipeline_log(folder: Path, **sections) -> None:
    log = read_pipeline_log(folder)
    log.update(sections)
    log['updated_at'] = datetime.now(KST).isoformat()
    write_json(folder / 'pipeline_log.json', log)



def body_results_log(folder: Path) -> list[dict]:
    entries = []
    for path in sorted((folder / 'bodies').glob('*.json')):
        result = json.loads(path.read_text(encoding='utf-8'))
        entries.append({
            **{key: result.get(key) for key in
               ('article_id', 'title', 'requested_url', 'fetched_url', 'retrieved_at',
                'reprocessed_at', 'processing_mode', 'body_status', 'body_character_count',
                'robots_result', 'http_status', 'http_requests', 'error')},
            'result_file': path.relative_to(folder).as_posix(),
            'html_file': path.with_suffix('.html').relative_to(folder).as_posix()
                         if path.with_suffix('.html').exists() else None,
        })
    return entries



def collection_folder(value: str | None = None) -> Path | None:
    root = (BASE / 'data').resolve()
    if value is None:
        manifest = BASE / 'web' / 'current.json'
        if manifest.exists():
            value = json.loads(manifest.read_text(encoding='utf-8'))['collectionId']
        else:
            # Read pre-pipeline snapshots, but never update this legacy pointer.
            pointer = root / 'latest_prism_news.txt'
            if not pointer.exists():
                return None
            value = pointer.read_text(encoding='utf-8').strip()
    if not isinstance(value, str) or not value:
        raise ValueError('Collection ID must be a nonempty folder name.')
    folder = (root / value).resolve()
    if folder.parent != root:
        raise ValueError('Collection folder must be directly inside Prism/data.')
    if not folder.is_dir():
        raise ValueError(f'Collection folder does not exist: {folder.name}')
    return folder



def load_bodies(folder: Path | None = None) -> dict[str, dict]:
    """Reuse body results across news snapshots, preferring extracted text."""
    bodies = {}
    for path in sorted((BASE / 'data').glob('*/bodies/*.json')):
        if folder and path.parent.parent == folder:
            continue
        try:
            body = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(body, dict) or not body.get('article_id'):
                continue
        except (OSError, ValueError):
            continue  # An unrelated damaged cache must not break the current run.
        article_id = body['article_id']
        previous = bodies.get(article_id, {})
        rank = lambda row: (row.get('body_status') == 'extracted_unverified',
                            row.get('reprocessed_at') or row.get('retrieved_at', ''))
        if not previous or rank(body) > rank(previous):
            bodies[article_id] = body
    if folder:
        for path in sorted((folder / 'bodies').glob('*.json')):
            body = json.loads(path.read_text(encoding='utf-8'))
            bodies[body['article_id']] = body
    return bodies
