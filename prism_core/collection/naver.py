"""Naver news requests and transient-error retries."""
from __future__ import annotations

import json
import time
from urllib.parse import urlencode

from prism_core.common.config import redact
from prism_core.common.transport import fetch


def search_request(query: str, start: int, endpoint: str, *,
                   display: int = 100, sort: str = 'date') -> dict:
    parameters = {'query': query, 'display': display, 'start': start, 'sort': sort, 'format': 'json'}
    return {'method': 'GET', 'url': endpoint + '?' + urlencode(parameters), 'parameters': parameters}



def query_api(query: str, start: int, headers: dict, endpoint: str, *,
              display: int = 100, sort: str = 'date') -> dict:
    url = search_request(query, start, endpoint, display=display, sort=sort)['url']
    for attempt in range(3):
        response = fetch(url, headers=headers)
        if response['status'] == 200:
            return json.loads(response['body'].decode('utf-8'))
        if response['status'] == 429 or response['status'] >= 500:
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
                continue
        detail = redact(response['body'].decode('utf-8', errors='replace')[:1000])
        raise RuntimeError(f"Naver HTTP {response['status']}: {detail}")
    raise RuntimeError('Retry attempts exhausted')
