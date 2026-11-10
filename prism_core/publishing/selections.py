"""Merge approved excerpts into the portal's normal articles and source lists."""
from __future__ import annotations

from datetime import date
from hashlib import sha256
import json
import re
from urllib.parse import urlsplit

from prism_core.common.files import read_jsonl
from prism_core.common.paths import BASE
from prism_core.common.text import canonical_url, document_key, normalized

METHOD = 'assistant_selection'
SELECTIONS = BASE / 'config' / 'article_selections.json'


def public_link(value):
    parts = urlsplit(value)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('Selected sources require HTTP(S) URLs without credentials.')
    return value


def identifier(url):
    return sha256(url.encode()).hexdigest()[:20]


def selected_comparisons(existing_releases=(), *, include_processed=True):
    if not SELECTIONS.exists():
        return None
    document = json.loads(SELECTIONS.read_text(encoding='utf-8'))
    if (document.get('version') != 1 or document.get('reviewer') != 'assistant' or
            document.get('userApproved') is not True or not isinstance(document.get('comparisons'), list)):
        raise ValueError('Selected comparisons require an approved, versioned configuration.')
    date.fromisoformat(document['reviewedAt'])
    revision = sha256(json.dumps(document, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    result = {'version': 1, 'method': METHOD, 'reviewer': 'assistant', 'userApproved': True,
              'reviewedAt': document['reviewedAt'], 'verificationMethod': document['verificationMethod'],
              'revision': revision, 'releases': [], 'articles': [], 'comparisons': []}
    seen, article_ids, release_ids = set(), set(), set()
    existing_sources = {document_key(release['url']): release for release in existing_releases if release.get('url')}
    for entry in document['comparisons']:
        key = entry['id']
        if not re.fullmatch(r'[a-z][a-z0-9-]{1,79}', key) or key in seen:
            raise ValueError('Selected comparison IDs must be unique lowercase slugs.')
        seen.add(key)
        for field in ('question', 'label', 'reason', 'qualification'):
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                raise ValueError('A selected comparison requires its question and context.')
        source = entry['release']
        source_url = public_link(source['url'])
        release_id = source.get('releaseId') or identifier(source_url)
        if not re.fullmatch(r'[a-f0-9]{20}', release_id):
            raise ValueError('Selected statistical source IDs must be 20 hexadecimal characters.')
        date.fromisoformat(source['publishedAt'])
        existing = existing_sources.get(document_key(source_url))
        if existing:
            if (existing['title'] != source['title'] or
                    not (existing.get('published_at') or '').startswith(source['publishedAt']) or
                    source.get('releaseId') and existing['release_id'] != source['releaseId']):
                raise ValueError('Selected statistical reference differs from the collected source.')
            release, release_id = existing, existing['release_id']
        else:
            release = {'release_id': release_id, 'title': source['title'], 'url': source_url,
                       'publisher': source['publisher'], 'published_at': source['publishedAt'],
                       'summary': source['summary'], 'metadata_status': METHOD}
            if release_id not in release_ids:
                result['releases'].append(release)
                release_ids.add(release_id)
        owners = []
        for side in ('A', 'B'):
            article = entry['article' + side]
            for field in ('title', 'publisher', 'speaker', 'position', 'summary', 'quote', 'sourceEvidence', 'sourceReview'):
                if not isinstance(article.get(field), str) or not article[field].strip():
                    raise ValueError('Selected articles require an excerpt, attribution and source evidence.')
            article_id = identifier(public_link(article['url']))
            public_link(article['verificationUrl'])
            date.fromisoformat(article['publishedAt'])
            if article_id in article_ids:
                raise ValueError('A selected article must occur once in the excerpt collection.')
            article_ids.add(article_id)
            owners.append({'id': article_id, 'title': article['title'], 'url': article['url'],
                'publisher': article['publisher'], 'providedAt': article['publishedAt'],
                'summary': article['summary'], 'body': article['quote'], 'bodyStatus': 'selected_excerpt',
                'imageUrl': '', 'speaker': article['speaker'], 'selectionId': key,
                'selectedExcerpt': True, 'matchedReleaseIds': [release_id],
                'rssTitles': [release['title']], 'releaseIds': [release_id],
                'candidateReleaseIds': [], 'sourceMatchStatus': 'supported',
                'queries': [], 'queryRanks': {}, 'duplicateOf': None,
                'sourceMatches': [{'releaseId': release_id, 'status': 'supported', 'basis': METHOD,
                                   'reason': article['sourceReview'], 'evidence': [article['sourceEvidence']]}],
                'claims': [{'text': article['quote'], 'kind': 'selected_excerpt', 'speaker': article['speaker']}],
                'relations': {'related': [], 'opposed': []}})
        for side, owner, other in (('A', owners[0], owners[1]), ('B', owners[1], owners[0])):
            owner['relations']['opposed'].append({'articleId': other['id'], 'releaseIds': [release_id],
                'basis': METHOD, 'score': 2.0, 'nliCompared': False, 'selectionId': key,
                'oppositionLabel': entry['label'], 'reason': entry['reason'],
                'qualification': entry['qualification'],
                'currentPosition': entry['article' + side]['position'],
                'otherPosition': entry['articleB' if side == 'A' else 'articleA']['position'],
                'currentEvidence': owner['body'], 'otherEvidence': other['body']})
        result['articles'].extend(owners)
        result['comparisons'].append({'id': key, 'question': entry['question'], 'label': entry['label'],
                                     'releaseId': release_id, 'articleA': owners[0]['id'], 'articleB': owners[1]['id']})
    if include_processed:
        apply_processed_sources(result)
    return result


def apply_processed_sources(selected):
    from prism_core.analysis.articles import load_analysis

    folder = BASE / 'data' / 'selected_sources'
    if not (folder / 'analysis.json').exists():
        raise ValueError('Process approved URLs first: python -m prism_core.collection.selected')
    source = json.loads((folder / 'source.json').read_text(encoding='utf-8'))
    if source.get('kind') != 'approved_direct_urls' or source.get('selectionRevision') != selected['revision']:
        raise ValueError('Approved URL inputs changed; run python -m prism_core.collection.selected')
    rows = read_jsonl(folder / 'articles.jsonl')
    releases = json.loads((folder / 'rss_releases.json').read_text(encoding='utf-8'))
    expected_urls = {article['id']: article['url'] for article in selected['articles']}
    if {row['article_id']: row['originallink'] for row in rows} != expected_urls:
        raise ValueError('Processed direct URLs differ from approved source URLs.')
    analysis = load_analysis(folder, rows, releases)
    if analysis is None:
        raise ValueError('Approved URL analysis is missing.')
    for article in selected['articles']:
        path = folder / 'bodies' / (article['id'] + '.json')
        body = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        if body and (body.get('article_id') != article['id'] or
                     body.get('selected_source_url', body.get('requested_url')) != article['url']):
            raise ValueError('A selected body cache belongs to a different URL.')
        text = body.get('body_text', '') if body.get('body_status') == 'extracted_unverified' else ''
        claim = article['claims'][0]
        if text:
            if normalized(claim['text']) not in normalized(text):
                raise ValueError('An approved quote is absent from its crawled body: ' + article['id'])
            claim['kind'] = 'body'
        details = analysis['articles'][article['id']]
        source_id = article['matchedReleaseIds'][0]
        supported = next((match for match in details['sourceMatches']
                          if match['releaseId'] == source_id and match['status'] == 'supported'), None)
        if supported:
            article['sourceMatches'] = [supported]
        article.update(body=text, bodyStatus=body.get('body_status', 'not_requested'),
                       bodyRetrievedAt=body.get('retrieved_at'), imageUrl=body.get('image_url') or '',
                       selectedExcerpt=False, collectionMethod='approved_direct_url',
                       claims=[claim, *[item for item in details['claims'] if item['text'] != claim['text']]],
                       automaticAnalysis=details,
                       bodyProcessing={key: body.get(key) for key in
                                       ('requested_url', 'fetched_url', 'body_status', 'http_status',
                                        'robots_result', 'processing_mode', 'extraction_library', 'extraction_method')})
    log = json.loads((folder / 'pipeline_log.json').read_text(encoding='utf-8'))
    selected['processing'] = log['selected_processing']
    if selected['processing']['inputFingerprint'] != analysis['inputFingerprint']:
        raise ValueError('Approved URL processing metrics differ from their analysis.')


def selection_metadata(selected):
    if selected is None:
        return None
    return {**{key: value for key, value in selected.items() if key not in ('articles', 'releases')},
            'articleIds': [article['id'] for article in selected['articles']],
            'releaseIds': [release['release_id'] for release in selected['releases']]}


def apply_selected_comparisons(payload):
    selected = selected_comparisons(payload['releases'])
    if selected is None:
        return
    if {release['release_id'] for release in payload['releases']} & {release['release_id'] for release in selected['releases']}:
        raise ValueError('Selected statistical source IDs conflict with collected sources.')
    # Preserve approved IDs and relations when the search API later finds the same URL.
    selected_urls = {canonical_url(article['url']): article for article in selected['articles']}
    selected_ids = {article['id']: article for article in selected['articles']}
    aliases, retained = {}, []
    for article in payload['articles']:
        chosen = selected_urls.get(canonical_url(article['url']))
        if article['id'] in selected_ids and chosen is not selected_ids[article['id']]:
            raise ValueError('A selected article ID belongs to a different collected URL.')
        if chosen:
            aliases[article['id']] = chosen['id']
        else:
            retained.append(article)
    for article in retained:
        duplicate = aliases.get(article.get('duplicateOf'), article.get('duplicateOf'))
        article['duplicateOf'] = duplicate if duplicate != article['id'] else None
        for kind, relations in article.get('relations', {}).items():
            seen, normalized_relations = set(), []
            for relation in relations:
                target = aliases.get(relation['articleId'], relation['articleId'])
                key = (target, tuple(sorted(relation['releaseIds'])))
                if target != article['id'] and key not in seen:
                    normalized_relations.append({**relation, 'articleId': target})
                    seen.add(key)
            article['relations'][kind] = normalized_relations
    payload['articles'] = [*retained, *selected['articles']]
    payload['releases'].extend(selected['releases'])
    payload['rssTitles'] = list(dict.fromkeys([*payload['rssTitles'], *[release['title'] for release in selected['releases']]]))
    payload['articleSelections'] = selection_metadata(selected)


def validate_selected_comparisons(payload):
    expected = selected_comparisons([release for release in payload['releases'] if release.get('metadata_status') != METHOD])
    if payload.get('articleSelections') != selection_metadata(expected):
        raise ValueError('Selected comparisons differ from their approved configuration.')
    if expected is None:
        return set(), set()
    article_ids = {article['id'] for article in expected['articles']}
    release_ids = {release['release_id'] for release in expected['releases']}
    for article in expected['articles']:
        if [item for item in payload['articles'] if item['id'] == article['id']] != [article]:
            raise ValueError('A selected article differs from its approved excerpt or relations.')
    for release in expected['releases']:
        if [item for item in payload['releases'] if item['release_id'] == release['release_id']] != [release]:
            raise ValueError('A selected statistical source differs from its approved reference.')
    if ({article['id'] for article in payload['articles'] if article.get('selectionId')} != article_ids or
            {release['release_id'] for release in payload['releases'] if release.get('metadata_status') == METHOD} != release_ids):
        raise ValueError('The portal contains an unapproved selected source.')
    return article_ids, release_ids
