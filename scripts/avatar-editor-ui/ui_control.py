import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get('ESI_CUBISM_UI_STATE', str(Path.cwd() / 'cubism-ui-state')))
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def tree():
    with OPENER.open('http://127.0.0.1:8769/tree', timeout=15) as response:
        nodes = json.load(response)
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / 'latest-ui-tree.json').write_text(json.dumps(nodes, ensure_ascii=False, indent=2))
    return nodes

def action(act, identifier, value='', **extra):
    body = urllib.parse.urlencode({'act': act, 'id': identifier, 'value': value, **extra}).encode()
    request = urllib.request.Request('http://127.0.0.1:8769/action', data=body, method='POST')
    with OPENER.open(request, timeout=10) as response:
        result = json.load(response)
    time.sleep(0.4)
    return result

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] in {'click', 'text', 'select'}:
        print(action(sys.argv[1], int(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else ''))
    pattern = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == 'find' else ''
    nodes = tree()
    for node in nodes:
        if pattern and not re.search(pattern, node['text'] + ' ' + node['class'], re.I):
            continue
        if not pattern and not node['text']:
            continue
        selected = {k: node[k] for k in ['id', 'parent', 'class', 'text', 'showing', 'enabled', 'x', 'y', 'w', 'h']}
        selected['text'] = selected['text'][:180]
        print(selected)
