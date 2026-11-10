"""Remove intermediate snapshots while retaining published collection results."""
from __future__ import annotations

import json
import re

from prism_core.common.locking import pipeline_lock
from prism_core.common.paths import BASE
from prism_core.common.storage import read_pipeline_log


def clean_snapshots(*, apply=False):
    """Keep the active snapshot and the last validated build of every collection."""
    with pipeline_lock():
        web = BASE / 'web'
        manifest = json.loads((web / 'current.json').read_text(encoding='utf-8'))
        retained = {}

        def retain(filename, collection_id):
            if not isinstance(filename, str) or not re.fullmatch(r'snapshots/news_[a-f0-9]{32}\.json', filename):
                raise ValueError('A retained snapshot has an invalid path.')
            path = web / filename
            payload = json.loads(path.read_text(encoding='utf-8'))
            if payload.get('schemaVersion') not in (1, 2) or not isinstance(payload.get('articles'), list) or not payload['articles']:
                raise ValueError('A retained snapshot has an unsupported structure.')
            if not isinstance(payload.get('rssTitles'), list):
                raise ValueError('A retained snapshot is missing its RSS titles.')
            if payload['meta']['collectionId'] != collection_id:
                raise ValueError('A retained snapshot does not match its collection.')
            retained[filename] = collection_id

        retain(manifest['data'], manifest['collectionId'])
        for folder in sorted((BASE / 'data').glob('news_*')):
            if not folder.is_dir():
                continue
            publication = read_pipeline_log(folder).get('publication', {})
            if publication.get('status') == 'validated':
                retain(publication.get('data'), folder.name)

        candidates = [
            path for path in sorted((web / 'snapshots').glob('news_*.json'))
            if re.fullmatch(r'news_[a-f0-9]{32}\.json', path.name)
            and path.relative_to(web).as_posix() not in retained
        ]
        total = sum(path.stat().st_size for path in candidates)
        removed = []
        if apply:
            for path in candidates:
                path.unlink()
                removed.append(path.relative_to(BASE).as_posix())
        return {
            'applied': apply,
            'kept': ['web/' + name for name in sorted(retained)],
            'candidates': [path.relative_to(BASE).as_posix() for path in candidates],
            'reclaimableBytes': total,
            'removed': removed,
        }
