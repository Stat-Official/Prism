"""Preserve RSS press-release metadata without asserting article source usage."""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urljoin, urlsplit

from prism_core.common.config import KST
from prism_core.common.text import canonical_url, clean_text, parse_timestamp

RELEASE_SCHEMA_VERSION = 1


def http_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
        return parts.scheme in ('http', 'https') and bool(parts.hostname) and not (
            parts.username or parts.password)
    except ValueError:
        return False


def release_date(raw: str) -> dict:
    """Keep timezone-free RSS dates timezone-free, including the agency's compact format."""
    result = {'published_at_raw': raw, 'published_at': '', 'published_at_status': 'missing',
              'published_at_precision': None, 'published_at_timezone': None}
    if not raw:
        return result
    stamp = parse_timestamp(raw)
    precision = 'second'
    if stamp is None:
        try:
            compact = {8: ('%Y%m%d', 'date'), 12: ('%Y%m%d%H%M', 'minute'),
                       14: ('%Y%m%d%H%M%S', 'second')}
            if raw.isdigit() and len(raw) in compact:
                pattern, precision = compact[len(raw)]
                stamp = datetime.strptime(raw, pattern)
            else:
                stamp = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                precision = 'date' if re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw) else 'second'
        except ValueError:
            result['published_at_status'] = 'unparsed'
            return result
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(KST)
        result['published_at_timezone'] = '+09:00'
    result.update(published_at=stamp.date().isoformat() if precision == 'date' else stamp.isoformat(),
                  published_at_status='parsed', published_at_precision=precision)
    return result


def identity(release: dict) -> tuple[str, str]:
    if release.get('metadata_status') == 'legacy_title_only':
        basis, value = 'legacy_title', release['title']
    elif release.get('guid'):
        basis, value = 'guid', release['guid']
    elif release.get('url'):
        basis, value = 'url', canonical_url(release['url'])
    else:
        basis, value = 'title_date', release['title'] + '\0' + release['published_at_raw']
    identifier = hashlib.sha256((release['feed_url'] + '\0' + basis + '\0' + value).encode()).hexdigest()[:20]
    return identifier, basis


def parse_rss(payload: bytes, source: dict) -> list[dict]:
    releases, seen = [], set()
    for item in ET.fromstring(payload).findall('.//{*}item'):
        fields = {node.tag.rsplit('}', 1)[-1]: node for node in item}

        def value(name):
            node = fields.get(name)
            return ''.join(node.itertext()).strip() if node is not None else ''

        title = clean_text(value('title'))
        if not title:
            continue
        raw_link, guid = value('link'), value('guid')
        link = urljoin(source['url'], raw_link) if raw_link else ''
        url_status = 'provided' if http_url(link) else 'invalid' if raw_link else 'missing'
        if url_status != 'provided':
            link = ''
            guid_node = fields.get('guid')
            if guid_node is not None and guid_node.get('isPermaLink', 'true').lower() != 'false' and http_url(guid):
                link, url_status = guid, 'guid_permalink'
        raw_date = value('pubDate') or value('date')
        release = {
            'title': title, 'url': link or None, 'url_raw': raw_link, 'url_status': url_status,
            'guid': guid or None, **release_date(raw_date),
            'rss_date_field': 'pubDate' if value('pubDate') else 'date' if value('date') else None,
            'publisher': source['publisher'], 'country': source['country'], 'feed_url': source['url'],
            'metadata_status': 'rss',
        }
        release['release_id'], release['identity_basis'] = identity(release)
        if release['release_id'] not in seen:
            releases.append(release)
            seen.add(release['release_id'])
    return releases


def legacy_releases(titles: list[str], source: dict) -> list[dict]:
    """Old snapshots retain title-only provenance; do not fetch or guess missing metadata."""
    releases = []
    for title in dict.fromkeys(titles):
        release = {
            'title': title, 'url': None, 'url_raw': '', 'url_status': 'missing', 'guid': None,
            **release_date(''), 'rss_date_field': None,
            'publisher': source['publisher'], 'country': source['country'], 'feed_url': source['url'],
            'metadata_status': 'legacy_title_only',
        }
        release['release_id'], release['identity_basis'] = identity(release)
        releases.append(release)
    return releases


def validate_releases(releases: list, source: dict) -> None:
    if not isinstance(releases, list) or not releases:
        raise ValueError('Press releases must be a nonempty list.')
    seen = set()
    for release in releases:
        if not isinstance(release, dict) or not isinstance(release.get('title'), str) or not release['title'].strip():
            raise ValueError('Press releases must have nonempty titles.')
        if any(release.get(field) != source[key] for field, key in
               [('feed_url', 'url'), ('publisher', 'publisher'), ('country', 'country')]):
            raise ValueError('Press-release source metadata differs from the saved collection source.')
        if release.get('url') is not None and not http_url(release['url']):
            raise ValueError('Press-release links must be HTTP(S) URLs without credentials.')
        if not isinstance(release.get('published_at_raw'), str):
            raise ValueError('Raw RSS dates must be strings.')
        expected_date = release_date(release['published_at_raw'])
        if any(release.get(key) != value for key, value in expected_date.items()):
            raise ValueError('Parsed press-release dates differ from their saved RSS values.')
        identifier, basis = identity(release)
        if release.get('release_id') != identifier or release.get('identity_basis') != basis or identifier in seen:
            raise ValueError('Press-release IDs must be unique and match the saved RSS identity.')
        seen.add(identifier)
