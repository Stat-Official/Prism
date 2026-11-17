"""Export the current portal as a single HTML file that opens without a server."""
from __future__ import annotations

import base64
import json
import re

from prism_core.common.files import atomic_write
from prism_core.common.paths import BASE


def _portal_data(web):
    manifest_path = web / 'current.json'
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        filename = manifest.get('data', '')
        if (manifest.get('schemaVersion') not in (1, 2) or
                not isinstance(filename, str) or
                not re.fullmatch(r'snapshots/news_[a-f0-9]{32}\.json', filename)):
            raise ValueError('Unsupported portal manifest.')
        data = json.loads((web / filename).read_text(encoding='utf-8'))
        if (data.get('schemaVersion') != manifest['schemaVersion'] or
                data.get('meta', {}).get('collectionId') != manifest.get('collectionId')):
            raise ValueError('Portal data does not match its manifest.')
    else:
        data = json.loads((web / 'demo.json').read_text(encoding='utf-8'))
        if data.get('schemaVersion') != 2 or data.get('meta', {}).get('mode') != 'curated_demo':
            raise ValueError('Unsupported comparison examples.')
    if any(not isinstance(data.get(key), list) for key in ('articles', 'releases', 'rssTitles')):
        raise ValueError('Portal data is incomplete.')
    return data


def _inline_json(data):
    # Prevent article text from ending the HTML data element.
    return (json.dumps(data, ensure_ascii=False, separators=(',', ':'))
            .replace('<', '\\u003c').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029'))


def export_html(*, output: str | None = None, force: bool = False):
    web = BASE / 'web'
    assets = web / 'assets'
    data = _portal_data(web)
    html = (web / 'index.html').read_text(encoding='utf-8')
    css = (assets / 'css/styles.css').read_text(encoding='utf-8')
    editorial_css = (assets / 'css/editorial.css').read_text(encoding='utf-8')

    def svg_url(name):
        encoded = base64.b64encode((assets / 'images' / name).read_bytes()).decode('ascii')
        return 'data:image/svg+xml;base64,' + encoded

    def module_source(name):
        source = (assets / 'js' / name).read_text(encoding='utf-8')
        return re.sub(r'^export\s+', '', source, flags=re.MULTILINE)

    app = (assets / 'js/app.js').read_text(encoding='utf-8')
    app, imports = re.subn(r'^import [^\n]+\n', '', app, flags=re.MULTILINE)
    if imports != 3:
        raise ValueError('The portal imports changed; update the standalone exporter.')
    app = app.replace('assets/images/cover.svg', svg_url('cover.svg'))
    javascript = '\n'.join([
        '(() => {\n"use strict";',
        module_source('format.mjs'),
        module_source('comparison-data.mjs'),
        'const esc = escapeHtml, date = formatDate;',
        'async function loadPortal() {\n'
        '  return JSON.parse(document.getElementById("prism-data").textContent);\n}',
        app,
        '})();',
    ])
    if '</script' in javascript.lower() or any('</style' in source.lower() for source in (css, editorial_css)):
        raise ValueError('Portal assets contain an unsupported HTML closing tag.')
    html, styles = re.subn(
        r'<link rel="stylesheet" href="assets/css/styles\.css[^\"]*">',
        lambda match: '<style>\n' + css + '\n</style>', html)
    html, editorials = re.subn(
        r'<link rel="stylesheet" href="assets/css/editorial\.css[^"]*">',
        lambda match: '<style>\n' + editorial_css + '\n</style>', html)
    html, scripts = re.subn(
        r'\s*<script src="assets/js/app\.js[^\"]*" type="module"></script>', '', html)
    if styles != 1 or editorials != 1 or scripts != 1:
        raise ValueError('The portal asset references changed; update the standalone exporter.')
    html = html.replace('assets/images/prism.svg', svg_url('prism.svg'))
    embedded = ('<script id="prism-data" type="application/json">' + _inline_json(data) +
                '</script>\n<script>\n' + javascript + '\n</script>\n')
    html = html.replace('</body>', embedded + '</body>', 1)
    destination = (BASE / (output or 'Prism.html')).resolve()
    if destination.parent != BASE.resolve() or destination.suffix.lower() != '.html':
        raise ValueError('Output must be an HTML filename in the project folder.')
    if destination.exists():
        previous = destination.read_text(encoding='utf-8')
        if previous == html:
            return destination
        if not force:
            raise FileExistsError(
                f'{destination.name} already exists with different content. '
                'Existing articles and custom design were preserved. '
                'Use --output Prism.preview.html for a separate preview '
                'or --force to explicitly replace the existing file.')
    atomic_write(destination, html)
    return destination
