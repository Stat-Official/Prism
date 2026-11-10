"""Deduplicate news candidates and export their search provenance."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from prism_core.common.config import KST
from prism_core.common.text import canonical_url, clean_text, parse_timestamp


def article_alias(value: str) -> str:
    url = canonical_url(value)
    parts = urlsplit(url)
    if parts.hostname and (parts.hostname == 'naver.com' or parts.hostname.endswith('.naver.com')):
        match = re.search(r'/(?:mnews/)?article/(\d+)/(\d+)', parts.path)
        params = dict(parse_qsl(parts.query))
        if match:
            return 'naver:' + ':'.join(match.groups())
        if params.get('oid') and params.get('aid'):
            return 'naver:' + params['oid'] + ':' + params['aid']
    return url



class Corpus:
    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.aliases: dict[str, str] = {}

    def add(self, item: dict, query: str, rank: int) -> None:
        links = [item.get('originallink', ''), item.get('link', '')]
        aliases = {article_alias(url) for url in links if url}
        if not aliases:
            return  # Raw response still retains this unusable result.
        previous = {self.aliases[alias] for alias in aliases if alias in self.aliases}
        if previous:
            chosen = sorted(previous)[0]
            row = self.rows[chosen]
            for duplicate in previous - {chosen}:
                other = self.rows.pop(duplicate)
                row['queries'] = sorted(set(row['queries']) | set(other['queries']))
                for matched_query, position in other['query_ranks'].items():
                    row['query_ranks'][matched_query] = min(row['query_ranks'].get(matched_query, position), position)
                for alias, owner in list(self.aliases.items()):
                    if owner == duplicate:
                        self.aliases[alias] = chosen
        else:
            chosen = hashlib.sha256(sorted(aliases)[0].encode()).hexdigest()[:20]
            row = self.rows[chosen] = {
                'article_id': chosen, 'title': clean_text(item.get('title', '')),
                'description': clean_text(item.get('description', '')),
                'originallink': item.get('originallink', ''), 'link': item.get('link', ''),
                'pubDate': item.get('pubDate', ''), 'queries': [], 'query_ranks': {},
            }
        row['queries'] = sorted(set(row['queries']) | {query})
        row['query_ranks'][query] = min(row['query_ranks'].get(query, rank), rank)
        for alias in aliases:
            self.aliases[alias] = chosen



def export_corpus(path: Path, corpus: Corpus) -> None:
    rows = sorted(corpus.rows.values(),
                  key=lambda row: parse_timestamp(row.get('pubDate', '')) or datetime.min.replace(tzinfo=KST),
                  reverse=True)
    with (path / 'articles.jsonl').open('w', encoding='utf-8') as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
