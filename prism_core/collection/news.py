"""Collect Naver news candidates using each RSS title and its publisher."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from prism_core.common.config import KST, api_config, redact, rss_config
from prism_core.common.files import write_json
from prism_core.common.locking import pipeline_lock
from prism_core.common.paths import BASE
from prism_core.common.storage import update_pipeline_log
from prism_core.common.text import clean_text
from prism_core.common.transport import fetch
from prism_core.collection.corpus import Corpus, export_corpus
from prism_core.collection.naver import query_api, search_request
from prism_core.collection.press_releases import RELEASE_SCHEMA_VERSION, parse_rss
from prism_core.collection.queries import query_tasks


def attach_provenance(corpus: Corpus, releases: list[dict], searches: list[dict]) -> None:
    for article in corpus.rows.values():
        ids = {rid for search in searches if search['query'] in article['queries'] for rid in search['release_ids']}
        matched = [release for release in releases if release['release_id'] in ids]
        article['release_ids'] = sorted(release['release_id'] for release in matched)
        article['rss_titles'] = sorted({release['title'] for release in matched})
        article['source_match_status'] = 'not_verified'


def collect(*, limit: int = 10, per_release: int = 30, rebuild: bool = False, query_mode: str = 'title') -> Path:
    if not 1 <= limit <= 50 or not 1 <= per_release <= 100:
        raise ValueError('limit must be 1..50 and per-release must be 1..100')
    endpoint, headers = api_config()
    if not all(headers.values()):
        raise RuntimeError("NAVER_CLIENT_ID and NAVER_CLIENT_SECRET are required.")
    source = rss_config()
    started = datetime.now(KST)
    folder = BASE / "data" / started.strftime("news_%Y%m%d_%H%M%S_%f")
    folder.mkdir(parents=True)
    (folder / "raw").mkdir()
    write_json(folder / "source.json", source)
    collection = {
        "status": "running", "started_at": started.isoformat(), "feed": source["url"],
        "query_mode": query_mode, "api": endpoint, "per_release": per_release,
        "rss_fields": ["title", "link", "guid", "pubDate", "date"],
        "release_schema_version": RELEASE_SCHEMA_VERSION,
        "unique_articles": 0,
        "source_use_verified": False, "article_bodies_collected": False,
    }
    searches = []
    corpus = Corpus()
    update_pipeline_log(folder, source=source, collection=collection, api_searches=searches)
    try:
        response = fetch(source["url"], {"User-Agent": "PrismNewsPilot/0.1"})
        if response["status"] != 200:
            raise RuntimeError(f'RSS HTTP {response["status"]}')
        (folder / 'raw' / 'rss.xml').write_bytes(response['body'])
        releases = [release for release in parse_rss(response['body'], source)
                    if not re.search(r"개최|채용|모집|워크숍|세미나|포럼|조사\s*실시", release['title'])][:limit]
        if not releases:
            raise RuntimeError("The selected RSS feed contains no usable press releases.")
        titles = list(dict.fromkeys(release['title'] for release in releases))
        write_json(folder / 'rss_releases.json', releases)
        write_json(folder / "rss_titles.json", titles)
        collection['selected_releases'] = len(releases)
        tasks = query_tasks(releases, source, query_mode)
        domains_by_release = {release['release_id']: set() for release in releases}
        for index, task in enumerate(tasks, 1):
            if task['conditional'] and all(len(domains_by_release[rid]) >= 4 for rid in task['release_ids']):
                continue
            query = task['query']
            result = {"query": query, "status": "pending", "returned_items": 0,
                      "variant": task['variant'],
                      "requested_at": datetime.now(KST).isoformat(),
                      "log_file": None,
                      "rss_titles": task['rss_titles'], "release_ids": task['release_ids'],
                      "request": search_request(query, 1, endpoint, display=per_release, sort="sim")}
            payload = None
            searches.append(result)
            update_pipeline_log(folder, collection=collection, api_searches=searches)
            try:
                before = set(corpus.rows)
                payload = query_api(query, 1, headers, endpoint, display=per_release, sort="sim")
                if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                    raise ValueError('Naver response must contain an items list.')
                for rank, item in enumerate(payload["items"], 1):
                    if not isinstance(item, dict):
                        raise ValueError('Naver items must be objects.')
                    if not clean_text(item.get("title", "")):
                        continue
                    if urlsplit(item.get("originallink") or item.get("link", "")).scheme not in {"http", "https"}:
                        continue
                    corpus.add(item, query, rank)
                    domain = urlsplit(item.get('originallink') or item.get('link', '')).hostname
                    if domain:
                        for rid in task['release_ids']:
                            domains_by_release[rid].add(domain.removeprefix('www.'))
                result.update(status="success", returned_items=len(payload.get("items", [])),
                              total_reported=payload.get("total", 0),
                              new_unique_articles=len(set(corpus.rows) - before),
                              new_article_ids=sorted(set(corpus.rows) - before),
                              publisher_domains={rid: len(domains_by_release[rid]) for rid in task['release_ids']})
                print(json.dumps({"query": index, "returned": result["returned_items"],
                                  "unique_so_far": len(corpus.rows)}), flush=True)
            except Exception as exc:
                result.update(status="failed", error=redact(str(exc)))
                if "HTTP 401" in str(exc) or "HTTP 403" in str(exc):
                    raise
            finally:
                result["finished_at"] = datetime.now(KST).isoformat()
                if payload is not None:
                    result["log_file"] = f"raw/{index:02d}.json"
                    write_json(folder / result["log_file"], {"query": query, "response": payload})
                collection["unique_articles"] = len(corpus.rows)
                update_pipeline_log(folder, collection=collection, api_searches=searches)
        attach_provenance(corpus, releases, searches)
        export_corpus(folder, corpus)
        collection.update(
            status="partial" if any(q["status"] == "failed" for q in searches) else "complete",
            finished_at=datetime.now(KST).isoformat(), unique_articles=len(corpus.rows),
        )
        if not corpus.rows:
            raise RuntimeError("No news candidates were returned; the previous homepage pool is retained.")
        update_pipeline_log(folder, collection=collection, api_searches=searches)
        write_json(folder / 'query_metrics.json', {'mode': query_mode, 'requests': len(searches),
                   'unique_articles': len(corpus.rows), 'searches': searches})
        if collection['status'] != 'complete':
            raise RuntimeError('Some news searches failed; the previous published result is retained.')
    except Exception as exc:
        collection.update(status="partial" if collection["status"] == "partial" else "failed",
                          error=redact(str(exc)), unique_articles=len(corpus.rows),
                          finished_at=datetime.now(KST).isoformat())
        update_pipeline_log(folder, collection=collection, api_searches=searches)
        raise
    if rebuild:
        from prism_core.pipeline import update
        update(run=folder.name)
    return folder


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument('--per-query', '--per-release', dest='per_release', type=int, default=30)
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument('--query-mode', choices=['adaptive', 'title'], default='adaptive')
    args = parser.parse_args()
    if not 1 <= args.limit <= 50 or not 1 <= args.per_release <= 100:
        parser.error("limit must be 1..50 and per-release must be 1..100")
    try:
        if args.no_build:
            with pipeline_lock():
                folder = collect(limit=args.limit, per_release=args.per_release, query_mode=args.query_mode)
        else:
            from prism_core.pipeline import update
            manifest = update(limit=args.limit, per_release=args.per_release, query_mode=args.query_mode)
            folder = BASE / 'data' / manifest['collectionId']
    except Exception as exc:
        print(redact(str(exc)), file=sys.stderr)
        return 1
    print("Saved RSS-title news search: " + str(folder))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
