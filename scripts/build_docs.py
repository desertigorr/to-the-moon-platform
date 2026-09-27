"""Generate standalone PyDoc pages and both OpenAPI contracts."""

import importlib
import json
from pathlib import Path
import pydoc
import re


def build():
    root = Path(__file__).resolve().parents[1]
    directory = root / 'docs/code'
    directory.mkdir(parents=True, exist_ok=True)
    modules = ['backend.app', 'backend.pipeline', 'backend.state', 'backend.schedule',
               'backend.geometry', 'backend.dispatch', 'backend.scenario', 'backend.emulator',
               'ndtp.protocol', 'ndtp.receiver', 'shared.contracts', 'ml_service.app', 'ml_service.explanations']
    renderer = pydoc.HTMLDoc()
    for name in modules:
        module = importlib.import_module(name)
        content = renderer.page(name, renderer.docmodule(module))
        content = re.sub(r'<a href="file:[^"]+">.*?</a>', name, content)
        (directory / f'{name}.html').write_text(content, encoding='utf-8')
    links = ''.join(f'<li><a href="{name}.html">{name}</a></li>' for name in modules)
    (directory/'index.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8">'
        '<title>To the Moon — документация кода</title><style>body{max-width:900px;margin:40px auto;'
        'font:17px/1.7 system-ui;color:#302b3c}a{color:#7052c1}</style><h1>Документация кода</h1>'
        '<p>Сгенерировано стандартным Python PyDoc. Контракты, классы и сигнатуры текущего решения.</p>'
        '<p><a href="/docs">Backend OpenAPI / Swagger</a></p><ul>'+links+'</ul></html>', encoding='utf-8')
    for module, filename in [('backend.app', 'openapi.json'), ('ml_service.app', 'ml-openapi.json')]:
        schema = importlib.import_module(module).create_app().openapi()
        (root/'docs'/filename).write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Generated {len(modules)} PyDoc pages and 2 OpenAPI contracts')


if __name__ == '__main__':
    build()
