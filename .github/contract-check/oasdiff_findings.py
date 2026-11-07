"""Convert `oasdiff changelog -f json` output into the common findings format.

Usage: oasdiff_findings.py <changelog.json> > findings.json
"""
import json
import sys

LEVELS = {3: 'ERR', 2: 'WARN', 1: 'INFO'}

with open(sys.argv[1]) as f:
    changes = json.load(f) or []

findings = [{
    'tool': 'oasdiff',
    'package': 'HTTP API',
    'level': LEVELS.get(c.get('level'), 'INFO'),
    'rule': c.get('id'),
    'target': f"{c.get('operation', '')} {c.get('path', '')}".strip(),
    'symbol': f"{c.get('operation', '')} {c.get('path', '')}".strip(),
    'kind': 'operation',
    'text': c.get('text'),
    'location': (c.get('source') or {}).get('file') if isinstance(c.get('source'), dict) else c.get('source'),
} for c in changes]

json.dump(findings, sys.stdout, indent=2)
print()
