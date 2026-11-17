#!/usr/bin/env python3
"""Require an explicit Ubuntu mapping when a macOS desktop action changes.

This guards review completeness, not behavioral correctness. Native event-loop
and shader contracts still require their numerical, GPU and desktop checks.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def audit():
    sources = [*sorted((ROOT / 'native').glob('Studio*.swift')),
               ROOT / 'native/WorkspaceController.swift', ROOT / 'native/App.swift']
    callbacks = {path.stem+'.'+name for path in sources for name in
        re.findall(r'@objc\s+(?:private\s+)?func\s+(\w+)\s*\(', path.read_text())}
    entries = json.loads((ROOT / 'ubuntu/feature-map.json').read_text())['actions']
    errors = []
    for key in sorted(callbacks-set(entries)):
        errors.append('Unreviewed macOS action: '+key)
    for key in sorted(set(entries)-callbacks):
        errors.append('Stale macOS action: '+key)
    symbols = {}
    for key, entry in entries.items():
        if not entry.get('behavior') or not entry.get('ubuntu'):
            errors.append('Missing behavior or Ubuntu entry: '+key)
        for target in entry.get('ubuntu', []):
            filename, name = target.split(':', 1)
            path = ROOT / 'ubuntu/gs_studio' / filename
            if path not in symbols:
                tree = ast.parse(path.read_text())
                symbols[path] = {node.name for node in ast.walk(tree)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
                symbols[path].update(node.attr for node in ast.walk(tree)
                    if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store))
            if name not in symbols[path]:
                errors.append('Missing Ubuntu symbol '+target+' for '+key)
    return {'status': 'failed' if errors else 'passed',
            'reviewed_macos_actions': len(callbacks), 'errors': errors}


if __name__ == '__main__':
    result = audit()
    print(json.dumps(result, indent=2))
    raise SystemExit(result['status'] != 'passed')
