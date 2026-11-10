"""Run the normal body, reference and NLI stages for approved direct URLs."""
from __future__ import annotations

import argparse
import json

from prism_core.analysis.articles import analyze_folder, load_analysis
from prism_core.collection.bodies import process_bodies
from prism_core.collection.references import collect_references
from prism_core.common.files import atomic_write, write_json
from prism_core.common.locking import pipeline_lock
from prism_core.common.paths import BASE
from prism_core.common.storage import collection_folder, update_pipeline_log

SELECTED_FOLDER = BASE / 'data' / 'selected_sources'


def process_selected_sources(releases, source, *, retry_failed=False, reextract=False, max_model_pairs=240):
    from prism_core.publishing.selections import selected_comparisons
    selected = selected_comparisons(releases, include_processed=False)
    if not selected or not selected['articles']:
        return None
    folder = SELECTED_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    known = {release['release_id']: release for release in [*releases, *selected['releases']]}
    used = {comparison['releaseId'] for comparison in selected['comparisons']}
    references = [known[key] for key in sorted(used)]
    rows = [{'article_id': article['id'], 'title': article['title'], 'description': article['summary'],
             'originallink': article['url'], 'link': article['url'], 'pubDate': article['providedAt'],
             'queries': [], 'query_ranks': {}, 'release_ids': article['releaseIds'],
             'rss_titles': article['rssTitles'], 'input_kind': 'approved_direct_url'}
            for article in selected['articles']]
    atomic_write(folder / 'articles.jsonl', ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))
    write_json(folder / 'rss_releases.json', references)
    write_json(folder / 'rss_titles.json', [release['title'] for release in references])
    write_json(folder / 'source.json', {'kind': 'approved_direct_urls', 'selectionRevision': selected['revision'],
                                      'rssContext': source})
    update_pipeline_log(folder, collection={'kind': 'approved_direct_urls', 'status': 'complete',
                                           'unique_articles': len(rows)}, api_searches=[])
    reference_counts = collect_references(folder, references, source, retry_failed=retry_failed)
    body_report = process_bodies(folder, limit=len(rows), retry_failed=retry_failed, reextract=reextract)
    try:
        analysis = load_analysis(folder, rows, references)
    except ValueError:
        analysis = None
    if analysis is None:
        analysis = analyze_folder(folder, references, rows, max_model_pairs=max_model_pairs)
    result = {'collectionId': folder.name, 'inputKind': 'approved_direct_urls', 'apiSearches': 0,
              'referenceStatuses': reference_counts, 'bodyStatuses': body_report['statuses'],
              'analysisVersion': analysis['version'], 'inputFingerprint': analysis['inputFingerprint'],
              'counts': analysis['counts'], 'model': analysis['model'], 'humanValidated': False}
    update_pipeline_log(folder, reference_processing=reference_counts, selected_processing=result,
                        analysis={'version': analysis['version'], 'counts': analysis['counts'],
                                  'inputFingerprint': analysis['inputFingerprint'], 'humanValidated': False})
    return result


def main():
    from prism_core.publishing.build import validate_collection
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--retry-failed', action='store_true')
    parser.add_argument('--reextract', action='store_true')
    args = parser.parse_args()
    with pipeline_lock():
        folder = collection_folder()
        if folder is None:
            raise ValueError('Run prism.py update before processing approved sources.')
        _, source, releases, _ = validate_collection(folder)
        result = process_selected_sources(releases, source, retry_failed=args.retry_failed, reextract=args.reextract)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
