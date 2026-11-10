"""Normalize news text, timestamps and source URLs."""
from __future__ import annotations

import html
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from prism_core.common.config import KST

TRACKING_KEYS = {'fbclid', 'gclid', 'nclick', 'nclicks'}


def normalized(text):
    return ' '.join(str(text or '').split())


def document_key(value):
    try:
        parts = urlsplit(value)
        query = parse_qs(parts.query)
        if parts.hostname and parts.hostname.removeprefix('www.') in ('mods.go.kr', 'kostat.go.kr') and query.get('list_no'):
            return 'agency:' + query['list_no'][0]
        return value.split('#')[0]
    except ValueError:
        return ''


def clean_text(text: str) -> str:
    return html.unescape(re.sub(r'<[^>]*>', '', text or '')).strip()



def parse_timestamp(value: str) -> datetime | None:
    try:
        result = parsedate_to_datetime(value)
        return result.astimezone(KST) if result.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None



def canonical_url(value: str) -> str:
    parts = urlsplit(html.unescape(value.strip()))
    query = [(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith('utm_') and key.lower() not in TRACKING_KEYS]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path,
                       urlencode(query), ''))
