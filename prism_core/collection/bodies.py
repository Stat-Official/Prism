"""Extract article bodies for the current Prism news snapshot."""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import trafilatura
from lxml import html as lxml_html
from trafilatura.utils import decode_file

from prism_core.common.config import KST, load_env, redact
from prism_core.common.files import read_jsonl, write_json
from prism_core.common.locking import pipeline_lock
from prism_core.common.paths import BASE
from prism_core.common.storage import body_results_log, collection_folder, load_bodies, update_pipeline_log
from prism_core.common.transport import fetch

AGENT = 'PrismNewsPilot/0.1'
REDIRECTS = {301, 302, 303, 307, 308}
BODY_SELECTORS = {
    'tbc.co.kr': "//*[@id='article_body']",
    'news1.kr': "//*[@id='articleBodyContent']",
}


def public_url(url):
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        return False
    if parts.username or parts.password or parts.hostname.lower() == 'localhost':
        return False
    try:
        return ipaddress.ip_address(parts.hostname).is_global
    except ValueError:
        return not parts.hostname.lower().endswith(('.local', '.internal', '.localhost'))


def select_articles(rows, limit, per_domain=6):
    selected, domains, seen = [], Counter(), set()
    queues = {}
    ranked = sorted(rows, key=lambda row: bool(re.search(
        r"회복|악화|부진|착시|우려|분석|해석|안정|위기|한계|불안", row['title'])), reverse=True)
    for row in ranked:
        for release_id in row.get('release_ids') or row.get('rss_titles') or ['all']:
            queues.setdefault(release_id, []).append(row)
    while len(selected) < limit:
        progressed = False
        for queue in queues.values():
            while queue:
                row = queue.pop(0)
                identifier = row['article_id']
                domain = (urlsplit(row['originallink'] or row['link']).hostname or '').removeprefix('www.')
                if identifier in seen or domains[domain] >= per_domain:
                    continue
                selected.append(row)
                seen.add(identifier)
                domains[domain] += 1
                progressed = True
                break
            if len(selected) >= limit:
                break
        if not progressed:
            break
    return selected


def representative_image(tree, url):
    for name in ('og:image:secure_url', 'og:image', 'twitter:image', 'twitter:image:src'):
        for value in tree.xpath(f'//meta[@property="{name}" or @name="{name}"]/@content'):
            image_url = urljoin(url, value.strip())
            if value.strip() and public_url(image_url):
                return image_url
    return None


def embedded_article_text(tree, url):
    """Read public Chosun article hydration data without executing page scripts."""
    if (urlsplit(url).hostname or '').removeprefix('www.') != 'chosun.com':
        return ''
    for raw in tree.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            metadata = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(metadata, dict) and metadata.get('isAccessibleForFree') is False:
            return ''
    for script in tree.xpath('//script/text()'):
        match = re.search(r'Fusion\.globalContent\s*=\s*', script)
        if not match:
            continue
        try:
            article, _ = json.JSONDecoder().raw_decode(script[match.end():])
        except ValueError:
            continue
        if (not isinstance(article, dict) or article.get('content_restrictions') or
                article.get('canonical_url') != urlsplit(url).path):
            continue
        paragraphs = []
        for element in article.get('content_elements', []):
            if element.get('type') == 'text' and isinstance(element.get('content'), str):
                node = lxml_html.fragment_fromstring(element['content'], create_parent=True)
                text = node.text_content().strip()
                if text:
                    paragraphs.append(text)
        return '\n'.join(paragraphs)
    return ''


def parse_body(payload, url):
    payload = decode_file(payload)
    tree = lxml_html.fromstring(payload)
    image_url = representative_image(tree, url)
    extracted = trafilatura.extract(
        payload, url=url, output_format='json', with_metadata=True,
        include_comments=False, include_tables=True)
    document = json.loads(extracted) if extracted else {}
    text = document.get('text', '').strip()
    method = 'trafilatura'
    selector = BODY_SELECTORS.get((urlsplit(url).hostname or '').removeprefix('www.'))
    if selector:
        nodes = tree.xpath(selector)
        if nodes:
            content = nodes[0]
            for noise in content.xpath('.//script|.//style|.//figure|.//nav|.//aside'):
                noise.drop_tree()
            for block in content.xpath('.//br|.//p|.//div'):
                block.tail = '\n' + (block.tail or '')
            publisher_text = '\n'.join(line.strip() for line in content.text_content().splitlines() if line.strip())
            if len(publisher_text) >= 200 or len(publisher_text) > len(text):
                text, method = publisher_text, 'publisher_dom'
    if len(text) < 200:
        embedded = embedded_article_text(tree, url)
        if embedded:
            text, method = embedded, 'publisher_embedded_json'
    links = {urljoin(url, value) for value in tree.xpath('//a/@href')}
    return {
        'body_status': ('extracted_unverified' if len(text) >= 200 else
                        'short_text_review' if text else 'extraction_failed'),
        'body_text': text, 'body_character_count': len(text),
        'image_url': image_url,
        'publisher_date_extracted': document.get('date'),
        'extracted_title': document.get('title'),
        'publisher_name_extracted': next(iter(tree.xpath('//meta[@property="og:site_name"]/@content')), None) or document.get('sitename'),
        'extraction_library': 'trafilatura',
        'extraction_method': method,
        'source_links': sorted(link for link in links if public_url(link))[:200],
        'kosis_links': sorted(link for link in links if
            (urlsplit(link).hostname or '').lower() == 'kosis.kr' or
            (urlsplit(link).hostname or '').lower().endswith('.kosis.kr')),
    }


def extract_body(row, storage, request, allowed):
    result = {
        'article_id': row['article_id'], 'title': row['title'],
        'requested_url': row['originallink'] or row['link'],
        'retrieved_at': datetime.now(KST).isoformat(),
        'body_status': 'pending', 'source_usage_verified': False,
        'processing_mode': 'http', 'http_requests': [],
    }
    url = result['requested_url']
    try:
        for _ in range(6):
            if not public_url(url):
                result['body_status'] = 'unsafe_url_skipped'
                break
            permitted, reason = allowed(url)
            result['robots_result'] = reason
            if not permitted:
                result['body_status'] = reason
                break
            attempt = {'url': url, 'requested_at': datetime.now(KST).isoformat()}
            result['http_requests'].append(attempt)
            response = request(url)
            attempt.update(http_status=response['status'], finished_at=datetime.now(KST).isoformat())
            result['http_status'], result['fetched_url'] = response['status'], url
            if response['status'] in REDIRECTS:
                url = urljoin(url, response['location'])
                continue
            if response['status'] != 200:
                result['body_status'] = 'http_error'
                break
            if len(response['body']) > 5_000_000:
                result['body_status'] = 'page_too_large'
                break
            if response.get('content_type') and 'html' not in response['content_type'].lower():
                result['body_status'] = 'unsupported_content'
                break
            (storage / (row['article_id'] + '.html')).write_bytes(response['body'])
            result.update(parse_body(response['body'], url))
            break
        else:
            result['body_status'] = 'too_many_redirects'
    except Exception as exc:
        result.update(body_status='fetch_or_extract_error', error=redact(str(exc))[:600])
        if result['http_requests'] and 'finished_at' not in result['http_requests'][-1]:
            result['http_requests'][-1].update(error=result['error'], finished_at=datetime.now(KST).isoformat())
    result['finished_at'] = datetime.now(KST).isoformat()
    return result


class CrawlSession:
    def __init__(self):
        self.robots_cache, self.last_request = {}, {}

    def request(self, url):
        origin = urlsplit(url).netloc
        time.sleep(max(0, 1 - (time.monotonic() - self.last_request.get(origin, 0))))
        try:
            return fetch(url, headers={'User-Agent': AGENT}, timeout=15)
        finally:
            self.last_request[origin] = time.monotonic()

    def allowed(self, url):
        parts = urlsplit(url)
        origin = parts.scheme + '://' + parts.netloc
        if origin not in self.robots_cache:
            robots_url = origin + '/robots.txt'
            try:
                response = self.request(robots_url)
            except OSError:
                self.robots_cache[origin] = (False, 'robots_unavailable')
                return False, 'robots_unavailable'
            for _ in range(4):
                if response['status'] not in REDIRECTS:
                    break
                robots_url = urljoin(robots_url, response['location'])
                if not public_url(robots_url):
                    return False, 'robots_redirect_unsafe'
                response = self.request(robots_url)
            if response['status'] == 404:
                self.robots_cache[origin] = (None, 'robots_not_found')
            elif response['status'] != 200:
                self.robots_cache[origin] = (False, 'robots_unavailable')
            else:
                text = response['body'].decode('utf-8', errors='replace')
                if '<html' in text.lower() or '<!doctype' in text.lower():
                    self.robots_cache[origin] = (False, 'robots_unparseable')
                else:
                    parser = RobotFileParser()
                    parser.parse(text.splitlines())
                    self.robots_cache[origin] = (parser, 'robots_checked')
        parser, reason = self.robots_cache[origin]
        if parser is False:
            return False, reason
        if parser is None or parser.can_fetch(AGENT, url):
            return True, reason
        return False, 'robots_disallowed'


def process_bodies(folder, *, limit=24, retry_failed=False, reextract=False):
    if limit < 1:
        raise ValueError('limit must be positive')
    load_env(BASE / '.env')
    corpus = read_jsonl(folder / 'articles.jsonl')
    cached = load_bodies(folder)
    storage = folder / 'bodies'
    storage.mkdir(exist_ok=True)
    if reextract:
        selected = [row for row in corpus if (storage / (row['article_id'] + '.html')).exists()][:limit]
    else:
        pending = [row for row in corpus if row['article_id'] not in cached or
                   (retry_failed and cached[row['article_id']]['body_status'] != 'extracted_unverified')]
        selected = select_articles(pending, limit)
    for row in corpus:
        identifier = row['article_id']
        path = storage / (identifier + '.json')
        if identifier in cached and not path.exists():
            write_json(path, cached[identifier])
    client = CrawlSession()

    for index, row in enumerate(selected, 1):
        if reextract:
            result = json.loads((storage / (row['article_id'] + '.json')).read_text(encoding='utf-8'))
            result.update(parse_body((storage / (row['article_id'] + '.html')).read_bytes(),
                                    result.get('fetched_url') or result['requested_url']))
            result['reprocessed_at'] = datetime.now(KST).isoformat()
            result['processing_mode'] = 'cached_html'
        else:
            result = extract_body(row, storage, client.request, client.allowed)
        write_json(storage / (row['article_id'] + '.json'), result)
        cached[row['article_id']] = result
        print(json.dumps({'progress': f'{index}/{len(selected)}',
                          'article_id': row['article_id'], 'status': result['body_status']}), flush=True)
    outputs = [cached[row['article_id']] for row in corpus if row['article_id'] in cached]
    counts = dict(Counter(row['body_status'] for row in outputs))
    report = {
        'finished_at': datetime.now(KST).isoformat(), 'attempted_this_run': len(selected),
        'mode': 'cached_html' if reextract else 'http',
        'total_articles': len(corpus), 'stored_results': len(outputs), 'statuses': counts,
        'full_body_completeness_verified': False,
        'selection': ('Saved HTML in corpus order.' if reextract else
                      'Round-robin across releases, at most 6 per publisher domain; analytical headlines first.'),
    }
    update_pipeline_log(folder, body_processing=report, crawling=body_results_log(folder))
    print(json.dumps({'attempted': len(selected), 'statuses': counts}), flush=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', help='Collection folder; defaults to the published collection')
    parser.add_argument('--limit', type=int, default=24)
    parser.add_argument('--retry-failed', action='store_true')
    parser.add_argument('--reextract', action='store_true', help='Re-extract saved HTML without network requests')
    parser.add_argument('--no-build', action='store_true')
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error('limit must be positive')
    try:
        folder = collection_folder(args.run)
        if folder is None:
            raise ValueError('Run python prism.py update to collect and publish news first.')
        if args.no_build:
            with pipeline_lock():
                from prism_core.publishing.build import validate_collection
                validate_collection(folder)
                process_bodies(folder, limit=args.limit, retry_failed=args.retry_failed, reextract=args.reextract)
        else:
            from prism_core.pipeline import update
            update(run=folder.name, body_limit=args.limit, retry_failed=args.retry_failed, reextract=args.reextract)
    except Exception as exc:
        print(redact(str(exc)), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
