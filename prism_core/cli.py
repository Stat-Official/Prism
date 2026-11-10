"""Collect, analyze and serve the Prism news portal."""
from __future__ import annotations

import argparse
import json
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from prism_core.common.config import redact
from prism_core.common.locking import pipeline_lock
from prism_core.common.paths import BASE
from prism_core.common.storage import collection_folder
from prism_core.pipeline import update


def serve(port=8000):
    handler = partial(SimpleHTTPRequestHandler, directory=str(BASE / 'web'))
    with ThreadingHTTPServer(('127.0.0.1', port), handler) as server:
        print(f'Prism: http://127.0.0.1:{port}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0



def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    command = commands.add_parser('update', help='Run the full pipeline and publish on success')
    command.add_argument('--limit', type=int, default=10)
    command.add_argument('--per-query', '--per-release', dest='per_release', type=int, default=30,
                         help='Maximum results per query; at most three queries per release')
    command.add_argument('--body-limit', type=int, default=60)
    command.add_argument('--query-mode', choices=['adaptive', 'title'], default='adaptive')
    command.add_argument('--analysis-pairs', type=int, default=240, help='Maximum source-matched article pairs for NLI')
    command.add_argument('--run', help='Reprocess this collection instead of collecting new news')
    command.add_argument('--retry-failed', action='store_true')
    command.add_argument('--reextract', action='store_true', help='Use saved article HTML; requires --run')
    command.add_argument('--serve', action='store_true', help='Serve the local portal after a successful update')
    command.add_argument('--port', type=int, default=8000)
    server = commands.add_parser('serve', help='Serve existing portal data on localhost')
    server.add_argument('--port', type=int, default=8000)
    commands.add_parser('prepare-model', help='Prepare pinned local Korean NLI weights')
    commands.add_parser('demo', help='Rebuild the offline preview from approved comparisons')
    cleanup = commands.add_parser('clean', help='List obsolete snapshots; keep current and last build per collection')
    cleanup.add_argument('--apply', action='store_true', help='Delete the listed intermediate snapshots')
    report = commands.add_parser('report', help='Print saved collection and analysis metrics')
    report.add_argument('--run')
    args = parser.parse_args()
    if hasattr(args, 'port') and not 1 <= args.port <= 65535:
        parser.error('port must be 1..65535')
    try:
        if args.command == 'serve':
            return serve(args.port)
        if args.command == 'demo':
            from prism_core.publishing.demo import build_demo
            print('Prepared comparison preview: ' + str(build_demo()))
            return 0
        if args.command == 'clean':
            from prism_core.publishing.cleanup import clean_snapshots
            print(json.dumps(clean_snapshots(apply=args.apply), ensure_ascii=False, indent=2))
            return 0
        if args.command == 'prepare-model':
            from prism_core.analysis.model import prepare_model
            with pipeline_lock():
                path = prepare_model()
            print('Prepared local model: ' + str(path))
            return 0
        if args.command == 'report':
            from prism_core.analysis.articles import load_analysis
            from prism_core.publishing.build import validate_collection
            folder = collection_folder(args.run)
            if folder is None:
                raise ValueError('No published collection exists.')
            _, _, releases, rows = validate_collection(folder)
            saved = load_analysis(folder, rows, releases)
            if saved is None:
                raise ValueError('No saved analysis exists; run prism.py update --run ' + folder.name)
            print(json.dumps({'collectionId': folder.name, 'counts': saved['counts'],
                              'retrieval': saved.get('retrievalMetrics', {}), 'model': saved['model'],
                              'humanValidated': False}, ensure_ascii=False, indent=2))
            return 0
        manifest = update(limit=args.limit, per_release=args.per_release, body_limit=args.body_limit,
                          run=args.run, retry_failed=args.retry_failed, reextract=args.reextract,
                          query_mode=args.query_mode, max_model_pairs=args.analysis_pairs)
    except KeyboardInterrupt:
        message = ('Update interrupted; the last committed portal data is retained.'
                   if args.command == 'update' else f'Prism {args.command} interrupted.')
        print(message, file=sys.stderr)
        return 130
    except Exception as exc:
        prefix = 'Update failed; the previous portal data is retained. ' if args.command == 'update' else f'Prism {args.command} failed. '
        print(prefix + redact(str(exc)), file=sys.stderr)
        return 1
    print(json.dumps({'status': 'published', **manifest}, ensure_ascii=False))
    if args.serve:
        try:
            return serve(args.port)
        except OSError as exc:
            print('Portal data was updated, but the local server could not start: ' + str(exc), file=sys.stderr)
            return 1
    return 0
