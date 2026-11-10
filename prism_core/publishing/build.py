"""Validate a selected collection and atomically publish its portal data."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from prism_core.common.config import KST, redact
from prism_core.common.files import read_jsonl, write_json
from prism_core.common.locking import pipeline_lock
from prism_core.common.paths import BASE
from prism_core.common.storage import body_results_log, collection_folder, load_bodies, read_pipeline_log, update_pipeline_log
from prism_core.common.text import parse_timestamp
from prism_core.collection.press_releases import RELEASE_SCHEMA_VERSION, legacy_releases, validate_releases
from prism_core.analysis.articles import load_analysis
from prism_core.collection.references import read_reference
from prism_core.publishing.selections import apply_selected_comparisons, validate_selected_comparisons

WEB = BASE / 'web'
SCHEMA_VERSION = 2


def snapshot_source(folder: Path, log: dict) -> dict:
    path = folder / 'source.json'
    source = json.loads(path.read_text(encoding='utf-8')) if path.exists() else log.get('source')
    if not isinstance(source, dict) or any(
        not isinstance(source.get(key), str) or not source[key].strip()
        for key in ('url', 'publisher', 'country')
    ):
        raise ValueError('This collection has no complete saved source metadata. '
                         'Collect it again; current .env values will not be substituted.')
    if log.get('collection', {}).get('feed') != source['url']:
        raise ValueError('Saved source URL does not match the collection feed.')
    return {key: source[key] for key in ('url', 'publisher', 'country')}


def validate_collection(folder: Path) -> tuple[dict, dict, list, list]:
    log = read_pipeline_log(folder)
    collection = log.get('collection', {})
    if collection.get('status') != 'complete':
        raise ValueError('Only a complete news collection can be published.')
    source = snapshot_source(folder, log)
    titles = json.loads((folder / 'rss_titles.json').read_text(encoding='utf-8'))
    if not isinstance(titles, list) or not titles or any(not isinstance(t, str) or not t for t in titles):
        raise ValueError('The RSS title list must contain nonempty titles.')
    release_path = folder / 'rss_releases.json'
    release_version = collection.get('release_schema_version')
    if release_version is not None and release_version != RELEASE_SCHEMA_VERSION:
        raise ValueError('Unsupported saved press-release schema.')
    if release_version is not None and not release_path.exists():
        raise ValueError('The collection is missing its saved press-release metadata.')
    has_release_metadata = release_path.exists()
    releases = (json.loads(release_path.read_text(encoding='utf-8')) if has_release_metadata
                else legacy_releases(titles, source))
    validate_releases(releases, source)
    release_map = {release['release_id']: release for release in releases}
    if {release['title'] for release in releases} != set(titles):
        raise ValueError('Saved press releases do not match the RSS title list.')
    if release_version is not None and (
        collection.get('selected_releases') != len(releases) or
        any(release['metadata_status'] != 'rss' for release in releases)
    ):
        raise ValueError('Press-release metadata is incomplete or differs from the collection log.')
    searches = log.get('api_searches', [])
    if not searches or any(s.get('status') != 'success' for s in searches):
        raise ValueError('All recorded news searches must have succeeded.')
    active = {title for search in searches for title in search.get('rss_titles', [])}
    if active != set(titles):
        raise ValueError('Every selected RSS title must have a recorded successful search.')
    query_titles, query_releases = {}, {}
    for search in searches:
        query = search.get('query')
        if not isinstance(query, str) or not query.strip():
            raise ValueError('Recorded searches must have nonempty queries.')
        query_titles.setdefault(query, set()).update(search.get('rss_titles', []))
        release_ids = search.get('release_ids')
        if release_ids is None and not has_release_metadata:
            release_ids = [r['release_id'] for r in releases if r['title'] in search.get('rss_titles', [])]
        if not isinstance(release_ids, list) or not release_ids or any(rid not in release_map for rid in release_ids):
            raise ValueError('Every search must refer to saved press-release IDs.')
        if {release_map[rid]['title'] for rid in release_ids} != set(search.get('rss_titles', [])):
            raise ValueError('Search titles differ from their saved press-release IDs.')
        query_releases.setdefault(query, set()).update(release_ids)
    if set().union(*query_releases.values()) != set(release_map):
        raise ValueError('Every press release must have a recorded successful search.')
    rows = read_jsonl(folder / 'articles.jsonl')
    if not rows or collection.get('unique_articles') != len(rows):
        raise ValueError('Article count is empty or differs from the collection log.')
    normalized_rows = []
    for row in rows:
        queries = row.get('queries')
        if not isinstance(queries, list) or not queries or any(q not in query_titles for q in queries):
            raise ValueError('Article queries must refer to recorded searches.')
        expected_titles = set().union(*(query_titles[q] for q in queries))
        if set(row.get('rss_titles', [])) != expected_titles:
            raise ValueError('Article RSS provenance does not match its recorded searches.')
        expected_ids = set().union(*(query_releases[q] for q in queries))
        release_ids = row.get('release_ids')
        if release_ids is None and not has_release_metadata:
            release_ids = sorted(expected_ids)
        if not isinstance(release_ids, list) or set(release_ids) != expected_ids or len(release_ids) != len(expected_ids):
            raise ValueError('Article press-release IDs do not match its recorded searches.')
        normalized_rows.append({**row, 'release_ids': release_ids})
    return log, source, releases, normalized_rows


def validate_payload(payload: dict) -> None:
    if payload.get('schemaVersion') != SCHEMA_VERSION or not payload.get('articles'):
        raise ValueError('Portal payload must have a supported schema and nonempty articles.')
    ids = set()
    releases = payload['releases']
    selected_article_ids, selected_release_ids = validate_selected_comparisons(payload)
    validate_releases([release for release in releases if release['release_id'] not in selected_release_ids], payload['meta']['source'])
    release_map = {release['release_id']: release for release in releases}
    titles = set(payload['rssTitles'])
    if titles != {release['title'] for release in releases}:
        raise ValueError('Portal RSS titles do not match its press releases.')
    for article in payload['articles']:
        identifier = article.get('id', '')
        if not re.fullmatch(r'[a-f0-9]{20}', identifier) or identifier in ids:
            raise ValueError('Article IDs must be unique 20-character hexadecimal strings.')
        ids.add(identifier)
        if not article['title'].strip():
            raise ValueError('Article titles must be nonempty.')
        parts = urlsplit(article['url'])
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            raise ValueError('Article links must be HTTP(S) URLs without credentials.')
        if not article['rssTitles'] or not set(article['rssTitles']).issubset(titles):
            raise ValueError('Every article must refer to a selected RSS title.')
        release_ids = article.get('releaseIds')
        if not isinstance(release_ids, list) or not release_ids or len(set(release_ids)) != len(release_ids) or any(rid not in release_map for rid in release_ids):
            raise ValueError('Every article must refer to unique, known press-release IDs.')
        if {release_map[rid]['title'] for rid in release_ids} != set(article['rssTitles']):
            raise ValueError('Portal article titles differ from their press-release IDs.')
        if article.get('sourceMatchStatus') not in ('not_verified', 'supported'):
            raise ValueError('Unsupported source-evidence status.')
        if not set(article.get('matchedReleaseIds', [])).issubset(set(release_ids)):
            raise ValueError('Source-matched release IDs must be included in article references.')
        if identifier not in selected_article_ids and (not article['queries'] or any(
            not isinstance(article['queryRanks'].get(query), int) or article['queryRanks'][query] < 1
            for query in article['queries']
        )):
            raise ValueError('Every article query must have a positive search rank.')


def build(folder: Path) -> dict:
    """The manifest replacement is the only publication commit point."""
    log, source, releases, rows = validate_collection(folder)
    analysis = load_analysis(folder, rows, releases)
    if log.get('analysis') and analysis is None:
        raise ValueError('This collection is missing its article analysis.')
    releases = [{**release, 'reference': {key: reference.get(key) for key in
                  ('status', 'announced_at', 'text', 'content_hash')}}
                for release in releases for reference in [read_reference(folder, release)]]
    titles = list(dict.fromkeys(release['title'] for release in releases))
    collection = log['collection']
    rss_log = []
    for release in releases:
        queries = []
        for search in log['api_searches']:
            matched = (release['release_id'] in search['release_ids'] if 'release_ids' in search
                       else release['title'] in search.get('rss_titles', []))
            if matched:
                queries.append(search['query'])
        rss_log.append({'release_id': release['release_id'], 'title': release['title'],
                        'collection_queries': queries})
    bodies = ({path.stem: json.loads(path.read_text(encoding='utf-8'))
               for path in (folder / 'bodies').glob('*.json')} if analysis else load_bodies(folder))
    relations = {row['article_id']: {'related': [], 'opposed': []} for row in rows}
    if analysis:
        for comparison in analysis['comparisons']:
            for side, other in [('A', 'B'), ('B', 'A')]:
                relations[comparison['article' + side]][comparison['relation']].append({
                    'articleId': comparison['article' + other], 'releaseIds': comparison['releaseIds'],
                    'reason': comparison['reason'], 'basis': comparison['basis'], 'score': comparison['score'],
                    'currentEvidence': comparison['evidence' + side], 'otherEvidence': comparison['evidence' + other],
                    'nliCompared': comparison.get('nliCompared', False)})
    articles = []
    for article in rows:
        cached = bodies.get(article['article_id'], {})
        body = cached.get('body_text', '') if cached.get('body_status') == 'extracted_unverified' else ''
        stamp = parse_timestamp(article.get('pubDate', ''))
        domain = urlsplit(article['originallink'] or article['link']).hostname or ''
        details = analysis['articles'][article['article_id']] if analysis else {}
        matched = details.get('matchedReleaseIds', [])
        all_release_ids = sorted(set(article['release_ids']) | set(matched))
        articles.append({
            'id': article['article_id'], 'title': article['title'],
            'summary': article['description'], 'url': article['originallink'] or article['link'],
            'imageUrl': cached.get('image_url') or '',
            'publisher': cached.get('publisher_name_extracted') or domain.removeprefix('www.'),
            'providedAt': stamp.isoformat() if stamp else '',
            'body': body, 'bodyStatus': cached.get('body_status', 'not_requested'),
            'bodyRetrievedAt': cached.get('retrieved_at'),
            'rssTitles': sorted({r['title'] for r in releases if r['release_id'] in all_release_ids}),
            'releaseIds': all_release_ids, 'candidateReleaseIds': article['release_ids'],
            'matchedReleaseIds': matched, 'sourceMatchStatus': 'supported' if matched else 'not_verified',
            'sourceMatches': details.get('sourceMatches', []), 'claims': details.get('claims', []),
            'duplicateOf': details.get('duplicateOf'),
            'relations': {kind: sorted(items, key=lambda item: item['score'], reverse=True)
                          for kind, items in relations[article['article_id']].items()},
            'queries': article.get('queries', []), 'queryRanks': article.get('query_ranks', {}),
        })
    metrics = [{'query': search['query'], 'variant': search.get('variant', 'title'),
                'returned': search.get('returned_items', 0), 'newUnique': search.get('new_unique_articles'),
                'releaseIds': search.get('release_ids', [])} for search in log['api_searches']]
    payload = {
        'schemaVersion': SCHEMA_VERSION,
        'meta': {'collectionId': folder.name, 'builtAt': datetime.now(KST).isoformat(),
                 'collectedAt': collection.get('finished_at'), 'queryMode': collection.get('query_mode'),
                 'source': source, 'analysis': {'counts': analysis['counts'], 'model': analysis['model'],
                                              'retrieval': analysis.get('retrievalMetrics', {}),
                                              'humanValidated': False} if analysis else None},
        'rssTitles': titles, 'releases': releases, 'articles': articles, 'queryMetrics': metrics,
    }
    apply_selected_comparisons(payload)
    articles = payload['articles']
    validate_payload(payload)
    filename = 'snapshots/news_' + uuid4().hex + '.json'
    write_json(WEB / filename, payload)
    # Read back the serialized artifact before making it visible to readers.
    validate_payload(json.loads((WEB / filename).read_text(encoding='utf-8')))
    crawl_log = body_results_log(folder)
    selection = payload.get('articleSelections', {})
    counts = {'rss_items': len(releases) - len(selection.get('releaseIds', [])), 'collected_articles': len(rows),
              'portal_articles': len(articles), 'crawl_results_in_this_folder': len(crawl_log),
              'portal_articles_with_body': sum(bool(a['body']) for a in articles),
              'selected_comparison_pairs': len(selection.get('comparisons', [])),
              'selected_articles': len(selection.get('articleIds', [])),
              'selected_articles_with_body': sum(bool(a['body']) and bool(a.get('selectionId')) for a in articles)}
    update_pipeline_log(
        folder, portal_built_at=payload['meta']['builtAt'], counts=counts,
        rss_extraction=rss_log, crawling=crawl_log,
        publication={'status': 'validated', 'data': filename, 'counts': counts,
                     'note': 'web/current.json determines which artifact is published.'},
        pipeline={**log.get('pipeline', {}), 'status': 'validated',
                  'stage': 'publication_commit', 'finished_at': payload['meta']['builtAt']},
    )
    manifest = {'schemaVersion': SCHEMA_VERSION, 'collectionId': folder.name,
                'data': filename, 'publishedAt': payload['meta']['builtAt']}
    # Do not add fallible file operations after this single atomic commit.
    write_json(WEB / 'current.json', manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', help='Collection folder; defaults to the published collection')
    args = parser.parse_args()
    try:
        with pipeline_lock():
            folder = collection_folder(args.run)
            if folder is None:
                raise ValueError('Run python prism.py update to collect and publish news first.')
            manifest = build(folder)
    except Exception as exc:
        print(redact(str(exc)), file=sys.stderr)
        return 1
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
