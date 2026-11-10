"""Bounded query expansion and topic/period profiles for statistical releases."""
from __future__ import annotations

import re

TOPICS = [
    ('소비자물가', ('소비자물가', '물가상승률')),
    ('고용동향', ('고용동향', '취업자 고용')),
    ('근로형태', ('근로형태별 부가조사', '비정규직 근로자')),
    ('경제활동인구', ('경제활동인구조사', '취업자 고용')),
    ('기업생멸', ('기업생멸행정통계', '신생 소멸 기업')),
    ('생활시간', ('생활시간조사', '청년 혼인 자녀 시간사용')),
    ('가축동향', ('가축동향', '가축 사육 마릿수')),
    ('이주배경인구', ('이주배경인구', '이주배경 인구')),
    ('고령자', ('고령자 통계', '고령인구 노인')),
    ('산업활동', ('산업활동동향', '산업생산 소비 투자')),
    ('인구동향', ('인구동향', '출생 혼인 사망')),
    ('국내인구이동', ('국내인구이동', '인구 이동')),
    ('인구주택', ('인구주택총조사', '인구 가구')),
    ('가계동향', ('가계동향', '가구 소득 소비')),
    ('온라인쇼핑', ('온라인쇼핑동향', '온라인쇼핑 거래액')),
    ('쌀', ('쌀 예상생산량', '쌀 생산량')),
    ('사교육비', ('사교육비 조사', '사교육비')),
    ('지역소득', ('지역소득', '지역내총생산')),
]


def compact(value: str) -> str:
    return re.sub(r'[^0-9a-z가-힣]', '', value.lower())


def profile(title: str) -> dict:
    year = re.search(r'(20\d{2})\s*년?', title)
    month = re.search(r'(?<!\d)(1[0-2]|[1-9])\s*월', title)
    quarter = re.search(r'([1-4])\s*/?\s*4?분기', title)
    clean = re.sub(r'\([^)]*\)|\[[^]]*\]', ' ', title)
    clean = re.sub(r'20\d{2}\s*년?|(?<!\d)\d{1,2}\s*월|[1-4]\s*/?\s*4?분기', ' ', clean)
    clean = re.sub(r'보도자료|조사\s*결과|통계\s*결과|결과|공표|발표', ' ', clean)
    clean = ' '.join(clean.split()).strip(' -·') or title
    aliases = next((list(terms) for key, terms in TOPICS if key in compact(title)), [clean])
    return {'year': year.group(1) if year else None,
            'month': int(month.group(1)) if month else None,
            'quarter': int(quarter.group(1)) if quarter else None,
            'topic': aliases[0], 'aliases': aliases, 'core': clean}


def plan_queries(release: dict, source: dict, mode: str = 'adaptive') -> list[dict]:
    baseline = {'query': release['title'] + ' ' + source['publisher'], 'variant': 'title', 'conditional': False}
    if mode == 'title':
        return [baseline]
    if mode != 'adaptive':
        raise ValueError('query-mode must be adaptive or title')
    info = profile(release['title'])
    period = f"{info['month']}월" if info['month'] else f"{info['quarter']}분기" if info['quarter'] else info['year'] or ''
    core = ' '.join(part for part in (period, info['topic'], source['publisher']) if part)
    expanded_period = ' '.join(part for part in (info['year'], period if period != info['year'] else '') if part)
    expanded = ' '.join(part for part in (expanded_period, info['aliases'][-1]) if part)
    planned = [baseline, {'query': core, 'variant': 'core', 'conditional': False},
               {'query': expanded, 'variant': 'expanded', 'conditional': True}]
    seen, result = set(), []
    for item in planned:
        if item['query'] not in seen:
            result.append(item)
            seen.add(item['query'])
    return result


def query_tasks(releases: list[dict], source: dict, mode: str) -> list[dict]:
    tasks = {}
    for release in releases:
        for item in plan_queries(release, source, mode):
            task = tasks.setdefault(item['query'], {**item, 'release_ids': [], 'rss_titles': []})
            task['conditional'] = task['conditional'] and item['conditional']
            if release['release_id'] not in task['release_ids']:
                task['release_ids'].append(release['release_id'])
            if release['title'] not in task['rss_titles']:
                task['rss_titles'].append(release['title'])
    return list(tasks.values())
