"""Collect source text only as evidence for statistical-source matching."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit

from lxml import html
from trafilatura.utils import decode_file
import trafilatura

from prism_core.collection.bodies import CrawlSession, REDIRECTS, public_url
from prism_core.common.config import KST, redact
from prism_core.common.files import write_json
from prism_core.common.paths import BASE

REFERENCE_VERSION = 1
AGENCY_HOSTS = {'mods.go.kr', 'www.mods.go.kr', 'kostat.go.kr', 'www.kostat.go.kr'}


def extract_reference(payload: bytes, url: str) -> dict:
    decoded = decode_file(payload)
    tree = html.fromstring(decoded)
    nodes = tree.xpath('//div[contains(concat(" ", normalize-space(@class), " "), " board_content ")]')
    text = '\n'.join(node.text_content().strip() for node in nodes) if nodes else trafilatura.extract(decoded, url=url, include_tables=True, include_comments=False) or ''
    text = '\n'.join(line.strip() for line in text.splitlines() if line.strip())
    announced = ''
    match = re.search(r'보도일시\s*[:：]\s*(\d{4})\.(\d{1,2})\.(\d{1,2})\.?\s*(?:\([^)]*\))?\s*(\d{1,2}):(\d{2})', text)
    if match:
        try:
            announced = datetime(*(int(value) for value in match.groups())).isoformat()
        except ValueError:
            pass
    return {'text': text, 'announced_at': announced,
            'content_hash': hashlib.sha256(text.encode()).hexdigest()}


def read_reference(folder, release):
    path = folder / 'references' / (release['release_id'] + '.json')
    if not path.exists():
        return {'status': 'not_requested', 'text': '', 'announced_at': ''}
    result = json.loads(path.read_text(encoding='utf-8'))
    if result.get('release_id') != release['release_id'] or result.get('requested_url') != release['url']:
        raise ValueError('Reference evidence does not belong to the selected press release.')
    if result.get('text') and result.get('content_hash') != hashlib.sha256(result['text'].encode()).hexdigest():
        raise ValueError('Reference text differs from its stored content hash.')
    return result


def collect_references(folder, releases, source, *, retry_failed=False):
    storage = folder / 'references'
    storage.mkdir(exist_ok=True)
    client = CrawlSession()
    counts = {}
    for release in releases:
        identifier = release['release_id']
        path = storage / (identifier + '.json')
        if path.exists():
            previous = read_reference(folder, release)
            if previous.get('status') == 'extracted' or not retry_failed:
                counts[previous['status']] = counts.get(previous['status'], 0) + 1
                continue
        result = {'version': REFERENCE_VERSION, 'release_id': identifier,
                  'requested_url': release['url'], 'retrieved_at': datetime.now(KST).isoformat(),
                  'text': '', 'announced_at': '', 'status': 'pending'}
        if not release['url']:
            result['status'] = 'no_link'
        elif urlsplit(source['url']).hostname not in AGENCY_HOSTS or urlsplit(release['url']).hostname not in AGENCY_HOSTS:
            result['status'] = 'unsupported_source'
        else:
            try:
                cached = None
                for candidate in sorted((BASE / 'data').glob('*/release_pages/' + identifier + '.html'), reverse=True):
                    metadata = candidate.with_suffix('.json')
                    if metadata.exists() and json.loads(metadata.read_text()).get('requested_url') == release['url']:
                        cached = candidate.read_bytes()
                        result['processing_mode'] = 'saved_html'
                        break
                if cached is None:
                    for candidate in sorted((BASE / 'data').glob('*/references/' + identifier + '.json'), reverse=True):
                        previous = json.loads(candidate.read_text())
                        evidence = candidate.with_suffix('.html')
                        if previous.get('requested_url') == release['url'] and previous.get('status') == 'extracted' and evidence.exists():
                            stamp = datetime.fromisoformat(previous['retrieved_at'])
                            if (datetime.now(KST) - stamp).total_seconds() < 86400:
                                cached = evidence.read_bytes()
                                result.update(processing_mode='cached_reference', retrieved_at=previous['retrieved_at'])
                                break
                if cached is None:
                    url = release['url']
                    for _ in range(6):
                        if not public_url(url) or urlsplit(url).hostname not in AGENCY_HOSTS:
                            raise ValueError('Reference redirects must stay on official agency hosts.')
                        permitted, reason = client.allowed(url)
                        result['robots_result'] = reason
                        if not permitted:
                            result['status'] = reason
                            break
                        response = client.request(url)
                        result.update(http_status=response['status'], fetched_url=url, processing_mode='http')
                        if response['status'] in REDIRECTS:
                            url = urljoin(url, response.get('location', ''))
                            continue
                        if response['status'] != 200:
                            result['status'] = 'http_error'
                        elif len(response['body']) > 5_000_000:
                            result['status'] = 'page_too_large'
                        elif response.get('content_type') and 'html' not in response['content_type'].lower():
                            result['status'] = 'unsupported_content'
                        else:
                            cached = response['body']
                        break
                    else:
                        result['status'] = 'too_many_redirects'
                if cached is not None:
                    result.update(extract_reference(cached, release['url']))
                    result['status'] = 'extracted' if len(result['text']) >= 30 else 'text_unavailable'
                    (storage / (identifier + '.html')).write_bytes(cached)
            except Exception as exc:
                result.update(status='reference_error', error=redact(str(exc))[:600], text='')
        write_json(path, result)
        counts[result['status']] = counts.get(result['status'], 0) + 1
        print(json.dumps({'reference': identifier, 'status': result['status']}, ensure_ascii=False), flush=True)
    return counts
