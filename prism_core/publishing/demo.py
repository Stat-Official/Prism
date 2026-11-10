"""Build a deterministic, offline preview of the two approved comparisons."""
from prism_core.common.files import write_json
from prism_core.common.paths import BASE
from prism_core.publishing.selections import selected_comparisons, selection_metadata


def demo_payload():
    selected = selected_comparisons(include_processed=False)
    if not selected or not selected['comparisons']:
        raise ValueError('The preview requires approved comparisons.')
    return {
        'schemaVersion': 2,
        'meta': {'collectionId': 'selected-comparison-demo', 'mode': 'curated_demo',
                 'collectedAt': None, 'reviewedAt': selected['reviewedAt'], 'analysis': None},
        'articles': selected['articles'], 'releases': selected['releases'],
        'rssTitles': [release['title'] for release in selected['releases']],
        'queryMetrics': [], 'articleSelections': selection_metadata(selected),
    }


def build_demo():
    path = BASE / 'web' / 'demo.json'
    write_json(path, demo_payload())
    return path


if __name__ == '__main__':
    print(build_demo())
