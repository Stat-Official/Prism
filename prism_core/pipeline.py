"""Run collection, evidence processing, analysis and atomic publication."""
from __future__ import annotations

from datetime import datetime

from prism_core.common.config import KST, redact
from prism_core.common.locking import pipeline_lock
from prism_core.common.storage import collection_folder, update_pipeline_log


def update(*, limit=10, per_release=30, body_limit=60, run=None,
           retry_failed=False, reextract=False, query_mode='adaptive', max_model_pairs=240):
    from prism_core.analysis.articles import analyze_folder
    from prism_core.publishing.build import build, validate_collection
    from prism_core.collection.news import collect
    from prism_core.collection.bodies import process_bodies
    from prism_core.collection.references import collect_references
    from prism_core.collection.selected import process_selected_sources

    if not 1 <= limit <= 50 or not 1 <= per_release <= 100 or body_limit < 1 or not 1 <= max_model_pairs <= 2000:
        raise ValueError('limit must be 1..50, per-query 1..100, body-limit positive, and analysis-pairs 1..2000.')
    if reextract and not run:
        raise ValueError('--reextract requires --run to select saved HTML.')
    with pipeline_lock():
        folder = None
        stage = 'collection'
        started = datetime.now(KST).isoformat()
        try:
            folder = collection_folder(run) if run else collect(limit=limit, per_release=per_release, query_mode=query_mode)
            stage = 'collection_validation'
            pipeline = {'status': 'running', 'started_at': started, 'stage': stage,
                        'mode': 'existing_collection' if run else 'new_collection'}
            update_pipeline_log(folder, pipeline=pipeline)
            _, source, releases, rows = validate_collection(folder)
            stage = 'reference_evidence'
            pipeline.update(stage=stage)
            update_pipeline_log(folder, pipeline=pipeline)
            reference_counts = collect_references(folder, releases, source, retry_failed=retry_failed)
            update_pipeline_log(folder, reference_processing=reference_counts)
            stage = 'body_processing'
            pipeline.update(stage=stage)
            update_pipeline_log(folder, pipeline=pipeline)
            process_bodies(folder, limit=body_limit, retry_failed=retry_failed, reextract=reextract)
            stage = 'article_analysis'
            pipeline.update(stage=stage)
            update_pipeline_log(folder, pipeline=pipeline)
            analysis = analyze_folder(folder, releases, rows, max_model_pairs=max_model_pairs)
            update_pipeline_log(folder, analysis={'version': analysis['version'], 'counts': analysis['counts'],
                                                'inputFingerprint': analysis['inputFingerprint'],
                                                'humanValidated': False})
            stage = 'selected_source_processing'
            pipeline.update(stage=stage)
            update_pipeline_log(folder, pipeline=pipeline)
            selected_processing = process_selected_sources(releases, source, retry_failed=retry_failed,
                                                           reextract=reextract, max_model_pairs=max_model_pairs)
            update_pipeline_log(folder, selected_source_processing=selected_processing)
            stage = 'build_and_publish'
            pipeline.update(stage=stage)
            update_pipeline_log(folder, pipeline=pipeline)
            manifest = build(folder)
        except Exception as exc:
            if folder is not None:
                try:
                    update_pipeline_log(folder, pipeline={
                        'status': 'failed', 'started_at': started, 'stage': stage,
                        'finished_at': datetime.now(KST).isoformat(), 'error': redact(str(exc)),
                    })
                except OSError:
                    pass  # Preserve the original failure if logging also fails.
            raise
    return manifest
