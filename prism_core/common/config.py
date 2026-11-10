"""Environment settings, API credentials and log redaction."""
from __future__ import annotations

import os
from datetime import timedelta, timezone

from prism_core.common.paths import BASE

KST = timezone(timedelta(hours=9))


def load_env(path):
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))



def redact(value: str) -> str:
    for key in ('NAVER_CLIENT_ID', 'NAVER_CLIENT_SECRET'):
        secret = os.environ.get(key)
        if secret:
            value = value.replace(secret, '[redacted]')
    return value



def api_config() -> tuple[str, dict[str, str]]:
    load_env(BASE / '.env')
    provider = os.environ.get('NAVER_API_PROVIDER', 'developers')
    client_id = os.environ.get('NAVER_CLIENT_ID', '')
    secret = os.environ.get('NAVER_CLIENT_SECRET', '')
    if provider == 'ncp':
        return 'https://naverapihub.apigw.ntruss.com/search/v1/news', {
            'X-NCP-APIGW-API-KEY-ID': client_id, 'X-NCP-APIGW-API-KEY': secret}
    if provider == 'developers':
        return 'https://openapi.naver.com/v1/search/news.json', {
            'X-Naver-Client-Id': client_id, 'X-Naver-Client-Secret': secret}
    raise ValueError('NAVER_API_PROVIDER must be ncp or developers')



def rss_config() -> dict[str, str]:
    load_env(BASE / '.env')
    keys = {'url': 'RSS_URL', 'publisher': 'RSS_PUBLISHER', 'country': 'RSS_COUNTRY'}
    source = {key: os.environ.get(name, '').strip() for key, name in keys.items()}
    missing = [name for key, name in keys.items() if not source[key]]
    if missing:
        raise ValueError('Missing RSS settings in .env: ' + ', '.join(missing))
    return source
