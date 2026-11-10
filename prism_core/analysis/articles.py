"""Evidence-backed source matching and conservative local claim comparison."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from difflib import SequenceMatcher
from urllib.parse import urlsplit

from prism_core.common.files import write_json
from prism_core.common.storage import read_pipeline_log
from prism_core.common.text import document_key, normalized, parse_timestamp
from prism_core.collection.queries import compact, profile
from prism_core.collection.references import read_reference
from prism_core.analysis.model import LocalNLI

ANALYSIS_VERSION = 3
EVALUATIONS = re.compile(r'(?<!사)회복|개선|악화|부진|둔화|확대|위축|안정|불안|호조|침체|과장|착시|한계|우려|의미|평가|해석|심화|완화|청신호|적신호|낙관|비관|위기|반등|긍정|부정|성장|실질|착각')
NUMBER = re.compile(r'\d[\d,]*(?:\.\d+)?\s*(?:조|억|만)?\s*(?:\d[\d,]*\s*)?(?:%p|%|명|가구|원|마리|포인트)')
GROUPS = {'청년': r'청년|20대|2030', '고령': r'고령|노인|노년|60세|65세', '여성': r'여성|여자',
          '남성': r'남성|남자', '건설': r'건설업', '제조': r'제조업', '내국인': r'내국인',
          '외국인': r'외국인', '농산물': r'농산물', '축산물': r'축산물', '에너지': r'석유|에너지|유가'}
METRICS = {'고용': r'고용|취업|일자리', '실업': r'실업', '물가': r'물가|인플레',
           '소득': r'소득|수입', '생산': r'생산|제조', '소비': r'소비|지출', '인구': r'인구|고령|노인',
           '사육': r'사육|가축|마릿수', '출생': r'출생|출산', '혼인': r'혼인|결혼'}
REGIONS = {'경남': r'경남|경상남도', '경북': r'경북|경상북도', '전남': r'전남|전라남도',
           '전북': r'전북|전라북도|전북특별자치도', '충남': r'충남|충청남도', '충북': r'충북|충청북도',
           '강원': r'강원', '제주': r'제주', '경기': r'경기도|경기지역|경기\s+(?:고용|취업|물가|인구)',
           **{name: name for name in ('서울', '부산', '대구', '대전', '광주', '인천', '울산', '세종')}}


def geography(text, title=''):
    found = {name for name, pattern in REGIONS.items() if re.search(pattern, text)}
    if found:
        return '|'.join(sorted(found))
    if re.search(r'전국|국내|우리나라|대한민국', text):
        return 'national'
    return '|'.join(sorted(name for name, pattern in REGIONS.items() if re.search(pattern, title))) or 'national'


def sentences(text):
    return [normalized(piece) for piece in re.split(r'\n+|(?<=[.!?])\s+', text or '') if len(normalized(piece)) >= 8]


def numbers(text):
    return {re.sub(r'[\s,]', '', match.group()) for match in NUMBER.finditer(text)} - {'0%', '1%', '100%', '1명', '0명'}


def input_fingerprint(folder):
    digest = hashlib.sha256()
    for name in ['articles.jsonl', 'rss_releases.json', 'rss_titles.json', 'source.json']:
        path = folder / name
        if path.exists():
            digest.update(name.encode() + b'\0' + path.read_bytes())
    for directory in ['bodies', 'references']:
        for path in sorted((folder / directory).glob('*.json')):
            digest.update(path.relative_to(folder).as_posix().encode() + b'\0' + path.read_bytes())
    return digest.hexdigest()


def scheduled_mention(text):
    """A release listed in an agenda is not evidence that its data was used."""
    return bool(re.search(
        r'^\s*[※△▲•·\-]*\s*\d{1,2}:\d{2}\b|'
        r'\(\s*\d{1,2}(?::\d{2}|시)(?:\s*\d{1,2}분)?\s*\)\s*$|'
        r'(?:발표|공표|공개)(?:할\s*예정|를?\s*앞두|한다|된다|할\s*계획)', text))


def precedes_release(article, release):
    """Compare calendar days only; RSS timestamps may have no timezone."""
    stamp = parse_timestamp(article.get('pubDate', ''))
    try:
        article_day = stamp.date() if stamp else date.fromisoformat(article.get('pubDate', '')[:10])
        release_day = date.fromisoformat((release.get('published_at') or '')[:10])
    except (ValueError, TypeError):
        return False
    return article_day < release_day


def match_source(article, body, release, reference):
    text = body.get('body_text', '') if body.get('body_status') == 'extracted_unverified' else ''
    info = profile(release['title'])
    corpus = normalized(text)
    passage = sentences(text)
    signature = compact(re.sub(r'\([^)]*\)|보도자료|결과', '', release['title']))
    topic_terms = [compact(term) for term in info['aliases']] + [compact(info['core'])]
    topic_passages = [s for s in passage if any(term and term in compact(s) for term in topic_terms)]
    links = body.get('source_links', [])
    direct = bool(release['url']) and any(document_key(link) == document_key(release['url']) for link in links)
    mentions = [s for s in passage if len(signature) >= 6 and signature in compact(s)]
    named = next((s for s in mentions if not scheduled_mention(s)), None)
    candidate = release['release_id'] in article.get('release_ids', [])
    if precedes_release(article, release) or (mentions and not named):
        future = precedes_release(article, release)
        return {'releaseId': release['release_id'], 'status': 'candidate' if candidate else 'unrelated',
                'basis': 'upcoming_release' if future else 'release_schedule',
                'reason': '자료 발표 이전 기사이거나 발표 일정만 언급해 자료 사용 근거로 인정하지 않았어요.',
                'evidence': mentions[:2], 'score': 30 if candidate else 0}
    if direct:
        return {'releaseId': release['release_id'], 'status': 'supported', 'basis': 'direct_link',
                'reason': '기사 페이지에서 해당 보도자료로 연결되는 링크를 찾았어요.',
                'evidence': [named or (topic_passages[0] if topic_passages else article['title'])], 'score': 100}
    if named:
        return {'releaseId': release['release_id'], 'status': 'supported', 'basis': 'named_release',
                'reason': '기사 본문에서 대상 기간을 포함한 보도자료 명칭을 찾았어요.', 'evidence': [named], 'score': 95}
    if text and info['month'] and topic_passages:
        dates = {(year, str(int(month))) for year, month in re.findall(
            r'(20\d{2})\s*년\s*(\d{1,2})\s*월', ' '.join(topic_passages))}
        if dates and (info['year'], str(info['month'])) not in dates:
            return {'releaseId': release['release_id'], 'status': 'rejected', 'basis': 'different_period',
                    'reason': '통계 주제를 언급한 문장의 대상 기간이 달라요.', 'evidence': topic_passages[:2], 'score': 0}
    shared = numbers(corpus) & numbers(reference.get('text', ''))
    period_ok = (not info['year'] or info['year'] in corpus) and (
        not info['month'] or bool(re.search(rf"(?<!\d){info['month']}\s*월", corpus))) and (
        not info['quarter'] or bool(re.search(rf"{info['quarter']}\s*분기", corpus)))
    agency = release['publisher'] in corpus or '통계청' in corpus
    if text and topic_passages and period_ok and agency and len(shared) >= 2:
        evidence = [s for s in passage if numbers(s) & shared][:3]
        return {'releaseId': release['release_id'], 'status': 'supported', 'basis': 'topic_period_numbers',
                'reason': '통계 주제·대상 기간과 보도자료 요약의 수치가 함께 일치해요.',
                'evidence': evidence, 'sharedNumbers': sorted(shared), 'score': 85}
    return {'releaseId': release['release_id'], 'status': 'candidate' if candidate else 'unrelated',
            'basis': 'search_only' if candidate else 'insufficient_evidence',
            'reason': '검색 기준으로 연결됐지만 같은 자료를 인용했다는 본문 근거가 부족해요.',
            'evidence': topic_passages[:1], 'score': 30 if candidate else 0}


def claim_candidates(article, body):
    text = body.get('body_text', '') if body.get('body_status') == 'extracted_unverified' else ''
    candidates = [{'text': normalized(article['title']), 'kind': 'headline'}]
    candidates.extend({'text': sentence, 'kind': 'body'} for sentence in sentences(text) if 12 <= len(sentence) <= 220)
    seen, result = set(), []
    for claim in sorted(candidates, key=lambda c: (bool(EVALUATIONS.search(c['text'])), c['kind'] == 'headline'), reverse=True):
        if claim['text'] not in seen:
            result.append(claim)
            seen.add(claim['text'])
    for claim in result:
        claim['geography'] = geography(claim['text'], article['title'])
    return result[:6]


def comparable_claims(a, b, *, require_evaluation=True):
    left, right = a['text'], b['text']
    if require_evaluation and (not EVALUATIONS.search(left) or not EVALUATIONS.search(right)):
        return False
    region_a, region_b = a.get('geography', geography(left)), b.get('geography', geography(right))
    if region_a != region_b or '|' in region_a:
        return False
    groups_a = {name for name, pattern in GROUPS.items() if re.search(pattern, left)}
    groups_b = {name for name, pattern in GROUPS.items() if re.search(pattern, right)}
    if groups_a != groups_b:
        return False
    metrics_a = {name for name, pattern in METRICS.items() if re.search(pattern, left)}
    metrics_b = {name for name, pattern in METRICS.items() if re.search(pattern, right)}
    if not metrics_a & metrics_b:
        return False
    basis_a = {token for token in ('전년', '전월', '전분기') if token in left}
    basis_b = {token for token in ('전년', '전월', '전분기') if token in right}
    if basis_a and basis_b and basis_a != basis_b:
        return False
    scopes = (r'(?<!\d)(20\d{2})\s*년', r'(?<!\d)(\d{1,2})\s*월',
              r'([1-4])\s*분기', r'(?<!\d)(\d{1,2})\s*[~∼–-]\s*(\d{1,2})\s*세')
    for pattern in scopes:
        values_a, values_b = set(re.findall(pattern, left)), set(re.findall(pattern, right))
        if values_a and values_b and values_a != values_b:
            return False
    return True


def evidence_exists(text, article, body):
    return bool(normalized(text)) and normalized(text) in normalized(article['title'] + ' ' + article.get('description', '') + ' ' + body.get('body_text', ''))


def analyze_folder(folder, releases, rows, *, engine=None, max_model_pairs=240):
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    bodies = {p.stem: json.loads(p.read_text(encoding='utf-8')) for p in (folder / 'bodies').glob('*.json')}
    references = {r['release_id']: read_reference(folder, r) for r in releases}
    article_map = {row['article_id']: row for row in rows}
    analyses = {}
    for row in rows:
        body = bodies.get(row['article_id'], {})
        matches = [match_source(row, body, release, references[release['release_id']]) for release in releases]
        analyses[row['article_id']] = {'sourceMatches': [m for m in matches if m['status'] != 'unrelated'],
            'matchedReleaseIds': [m['releaseId'] for m in matches if m['status'] == 'supported'],
            'claims': claim_candidates(row, body), 'duplicateOf': None}
    texts = [row['title'] + ' ' + row.get('description', '') for row in rows]
    try:
        vectors = TfidfVectorizer(analyzer='char', ngram_range=(2, 4), max_features=30000).fit_transform(texts)
        similarity = cosine_similarity(vectors)
    except ValueError:
        similarity = [[1.0 if i == j else 0.0 for j in range(len(rows))] for i in range(len(rows))]
    candidates = {}
    for i, a in enumerate(rows):
        aid = a['article_id']
        source_a = set(analyses[aid]['matchedReleaseIds'])
        rejected_a = {m['releaseId'] for m in analyses[aid]['sourceMatches'] if m['status'] == 'rejected'}
        scored = []
        for j, b in enumerate(rows):
            if i == j:
                continue
            bid = b['article_id']
            source_b = set(analyses[bid]['matchedReleaseIds'])
            supported = source_a & source_b
            shared = set(a.get('release_ids', [])) & set(b.get('release_ids', []))
            rejected_b = {m['releaseId'] for m in analyses[bid]['sourceMatches'] if m['status'] == 'rejected'}
            shared -= rejected_a | rejected_b
            if not supported and (not shared or similarity[i][j] < .08):
                continue
            score = float(similarity[i][j]) + (.35 if supported else 0)
            scored.append((score, j, sorted(supported or shared), bool(supported)))
        for score, j, ids, supported in sorted(scored, reverse=True)[:12]:
            key = tuple(sorted((i, j)))
            candidates[key] = {'score': score, 'releaseIds': ids, 'supported': supported}
    tasks, comparisons = [], []
    model_edges = 0
    for (i, j), candidate in sorted(candidates.items(), key=lambda item: item[1]['score'], reverse=True):
        a, b = rows[i], rows[j]
        aid, bid = a['article_id'], b['article_id']
        aa, ba = analyses[aid], analyses[bid]
        common = set(aa['matchedReleaseIds']) & set(ba['matchedReleaseIds'])
        comparison = {'articleA': aid, 'articleB': bid, 'releaseIds': candidate['releaseIds'],
            'relation': 'related', 'score': round(candidate['score'], 5),
            'basis': 'shared_source' if common else 'search_candidate',
            'reason': '같은 통계자료를 가리키는 본문 근거가 있어요.' if common else '같은 검색 기준에서 찾은 기사예요. 자료 일치 근거는 추가 확인이 필요해요.',
            'evidenceA': a['title'], 'evidenceB': b['title'], 'nliCompared': False}
        comparisons.append(comparison)
        domain_a = (urlsplit(a['originallink'] or a['link']).hostname or '').removeprefix('www.')
        domain_b = (urlsplit(b['originallink'] or b['link']).hostname or '').removeprefix('www.')
        if domain_a == domain_b and SequenceMatcher(None, compact(a['title']), compact(b['title'])).ratio() >= .9:
            time_a, time_b = parse_timestamp(a.get('pubDate', '')), parse_timestamp(b.get('pubDate', ''))
            older, newer = (aid, bid) if ((time_a.timestamp() if time_a else 0), aid) < ((time_b.timestamp() if time_b else 0), bid) else (bid, aid)
            analyses[older]['duplicateOf'] = newer
        full_bodies = all(bodies.get(identifier, {}).get('body_status') == 'extracted_unverified' for identifier in (aid, bid))
        if not common or not full_bodies or model_edges >= max_model_pairs:
            continue
        pairs = [(ca, cb) for ca in aa['claims'] for cb in ba['claims'] if comparable_claims(ca, cb)]
        pairs.sort(key=lambda pair: SequenceMatcher(None, compact(pair[0]['text']), compact(pair[1]['text'])).ratio(), reverse=True)
        if pairs:
            for ca, cb in pairs[:2]:
                tasks.append((comparison, ca['text'], cb['text'], True))
        else:
            ca, cb = aa['claims'][0], ba['claims'][0]
            tasks.append((comparison, ca['text'], cb['text'], False))
        model_edges += 1
    engine = engine or LocalNLI()
    scores = engine.scores([pair for _, a, b, _ in tasks for pair in ((a, b), (b, a))])
    for position, (comparison, a, b, eligible) in enumerate(tasks):
        forward, backward = scores[2 * position:2 * position + 2]
        comparison['nliCompared'] = True
        strength = min(forward['contradiction'], backward['contradiction'])
        if eligible and strength > comparison.get('candidateContradictionScore', -1):
            comparison['candidateContradictionScore'] = round(strength, 5)
        if eligible and strength >= .85 and not forward.get('input_truncated') and not backward.get('input_truncated'):
            if strength > comparison.get('contradictionScore', 0):
                comparison.update(relation='opposed', basis='nli_contradiction', score=round(1 + strength, 5),
                    contradictionScore=round(strength, 5), evidenceA=a, evidenceB=b,
                    reason='같은 자료를 사용하고 비교 대상·지표가 맞는 문장에서 대립 가능성을 찾았어요.',
                    modelScores={'forward': forward, 'backward': backward})
        elif comparison['relation'] != 'opposed' and max(forward['entailment'], backward['entailment']) >= .8 and max(forward['contradiction'], backward['contradiction']) < .2:
            comparison.update(basis='nli_related', evidenceA=a, evidenceB=b,
                reason='같은 자료를 다룬 문장에서 유사하거나 보완하는 설명을 찾았어요.')
    for comparison in comparisons:
        for side in ('A', 'B'):
            identifier = comparison['article' + side]
            if not evidence_exists(comparison['evidence' + side], article_map[identifier], bodies.get(identifier, {})):
                raise ValueError('A comparison quote is not present in its source article.')
    searches = read_pipeline_log(folder).get('api_searches', [])
    baseline_queries = {search['query'] for search in searches if search.get('variant', 'title') == 'title'}
    baseline_count = sum(bool(set(row.get('queries', [])) & baseline_queries) for row in rows)
    retrieval_metrics = {'queryRequests': len(searches), 'baselineUniqueArticles': baseline_count,
                         'totalUniqueArticles': len(rows), 'additionalUniqueArticles': len(rows) - baseline_count,
                         'note': 'Candidate coverage counts; not human-validated relevance or recall.'}
    result = {'version': ANALYSIS_VERSION, 'inputFingerprint': input_fingerprint(folder),
              'releaseIds': [release['release_id'] for release in releases],
              'model': engine.report(), 'articles': analyses, 'comparisons': comparisons,
              'counts': {'articles': len(rows), 'articlesWithSourceEvidence': sum(bool(a['matchedReleaseIds']) for a in analyses.values()),
                         'relatedPairs': sum(c['relation'] == 'related' for c in comparisons),
                         'opposedPairs': sum(c['relation'] == 'opposed' for c in comparisons),
                         'modelComparedPairs': model_edges},
              'retrievalMetrics': retrieval_metrics, 'humanValidated': False}
    write_json(folder / 'analysis.json', result)
    print(json.dumps({'analysis': result['counts']}, ensure_ascii=False), flush=True)
    return result


def load_analysis(folder, rows, releases=None):
    path = folder / 'analysis.json'
    if not path.exists():
        return None
    result = json.loads(path.read_text(encoding='utf-8'))
    if result.get('version') != ANALYSIS_VERSION or result.get('inputFingerprint') != input_fingerprint(folder):
        raise ValueError('Analysis is outdated; run python prism.py update --run ' + folder.name)
    ids = {row['article_id'] for row in rows}
    row_map = {row['article_id']: row for row in rows}
    bodies = {p.stem: json.loads(p.read_text(encoding='utf-8')) for p in (folder / 'bodies').glob('*.json')}
    if set(result.get('articles', {})) != ids:
        raise ValueError('Analysis article IDs do not match the collection.')
    if releases is not None:
        known = {release['release_id'] for release in releases}
        if set(result.get('releaseIds', [])) != known:
            raise ValueError('Analysis press-release IDs do not match the collection.')
        for details in result['articles'].values():
            if not set(details.get('matchedReleaseIds', [])).issubset(known) or any(m.get('releaseId') not in known for m in details.get('sourceMatches', [])):
                raise ValueError('Analysis contains an unknown press-release reference.')
    for comparison in result.get('comparisons', []):
        if comparison.get('articleA') not in ids or comparison.get('articleB') not in ids or comparison['articleA'] == comparison['articleB']:
            raise ValueError('Analysis contains an invalid article comparison.')
        if comparison.get('relation') not in ('related', 'opposed'):
            raise ValueError('Unknown article comparison relation.')
        for side in ('A', 'B'):
            identifier = comparison['article' + side]
            if not evidence_exists(comparison.get('evidence' + side, ''), row_map[identifier], bodies.get(identifier, {})):
                raise ValueError('Analysis evidence is not present in the corresponding article.')
        if comparison['relation'] == 'opposed':
            left = result['articles'][comparison['articleA']].get('matchedReleaseIds', [])
            right = result['articles'][comparison['articleB']].get('matchedReleaseIds', [])
            if not set(left) & set(right) or comparison.get('contradictionScore', 0) < .85:
                raise ValueError('Opposed claims require shared-source evidence and an NLI threshold.')
    return result
