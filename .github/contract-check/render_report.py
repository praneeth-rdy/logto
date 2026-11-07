"""Render one PR comment from all contract checks.

Usage:
  render_report.py --out report.md --verdict verdict.txt
      [--api STATUS --api-findings F.json]
      [--symbols STATUS --symbol-findings F.json --packages name=dir,... --diffs DIR]
      [--repo DIR]

STATUS is `success`, `failure` or `skipped` (the GitHub job result). The verdict file
gets the number of ERR findings plus failed checks, so the workflow can fail on it.
"""
import argparse
import json
import os
import subprocess
from collections import OrderedDict

ICON = {'ERR': '❌', 'WARN': '⚠️', 'INFO': 'ℹ️'}
ORDER = {'ERR': 0, 'WARN': 1, 'INFO': 2}
MARKER = '<!-- contract-report -->'

ap = argparse.ArgumentParser()
ap.add_argument('--out', required=True)
ap.add_argument('--verdict', required=True)
ap.add_argument('--api', default='skipped')
ap.add_argument('--api-findings')
ap.add_argument('--api-tool', default='oasdiff')
ap.add_argument('--symbols', default='skipped')
ap.add_argument('--symbol-findings')
ap.add_argument('--symbol-tool', default='api-extractor')
ap.add_argument('--packages', default='')
ap.add_argument('--diffs')
ap.add_argument('--repo', default='.')
args = ap.parse_args()


def load(path):
    if path and os.path.exists(path):
        with open(path) as f:
            text = f.read().strip()
        return json.loads(text) if text else []
    return []


api = load(args.api_findings) if args.api == 'success' else []
sym = load(args.symbol_findings) if args.symbols == 'success' else []
packages = dict(p.split('=', 1) for p in args.packages.split(',') if '=' in p)


def counts(fs):
    return {lvl: sum(f['level'] == lvl for f in fs) for lvl in ORDER}


def usages(symbol, pkg_dir):
    """In-repo files that mention the symbol outside its own package (name match only)."""
    try:
        out = subprocess.run(
            ['git', 'grep', '-l', '-w', symbol, '--', 'packages', f':(exclude){pkg_dir}'],
            cwd=args.repo, capture_output=True, text=True).stdout.split()
    except OSError:
        return []
    return out


def package_of(path):
    """Nearest folder above `path` that has a package.json."""
    d = os.path.dirname(path)
    while d and not os.path.exists(os.path.join(args.repo, d, 'package.json')):
        d = os.path.dirname(d)
    return d or path


def group(fs):
    groups = OrderedDict()
    for f in sorted(fs, key=lambda f: (f['package'], f['symbol'], ORDER[f['level']])):
        groups.setdefault((f['package'], f['symbol']), []).append(f)
    # Highest level first, like the overall verdict.
    return sorted(groups.items(), key=lambda kv: min(ORDER[f['level']] for f in kv[1]))


lines = [MARKER]
c_api, c_sym = counts(api), counts(sym)
total = {lvl: c_api[lvl] + c_sym[lvl] for lvl in ORDER}
failed = [name for name, st in (('HTTP API', args.api), ('TypeScript API', args.symbols)) if st == 'failure']

if failed:
    headline = f"❌ Contract check incomplete: {', '.join(failed)} check failed to run"
elif total['ERR']:
    headline = f"❌ {total['ERR']} breaking change(s)"
elif total['WARN']:
    headline = f"⚠️ No confirmed breaks, {total['WARN']} change(s) need review"
elif args.api == 'skipped' and args.symbols == 'skipped':
    headline = '✅ No contract surface touched'
else:
    headline = '✅ No breaking changes'
lines += [f'## Contract check: {headline}', '']


def status_cell(st, c):
    if st == 'skipped':
        return '⏭️ not affected', '–', '–', '–'
    if st == 'failure':
        return '💥 failed to run', '–', '–', '–'
    return '✅ ran', str(c['ERR']), str(c['WARN']), str(c['INFO'])


pkg_names = ', '.join(f'`{p}`' for p in packages) or 'published packages'
lines += [
    '| Contract | Tool | Status | ❌ ERR | ⚠️ WARN | ℹ️ INFO |',
    '|---|---|---|---|---|---|',
    '| HTTP API (`/api/swagger.json`) | ' + args.api_tool + ' | ' + ' | '.join(status_cell(args.api, c_api)) + ' |',
    f'| TypeScript public API ({pkg_names}) | ' + args.symbol_tool + ' | ' + ' | '.join(status_cell(args.symbols, c_sym)) + ' |',
    '',
]

if args.api == 'success':
    lines += ['### HTTP API', '']
    if not api:
        lines += ['_No API changes._', '']
    for (_, op), fs in group(api):
        lines.append(f'**`{op}`**')
        lines += [f"- {ICON[f['level']]} `{f['rule']}`: {f['text']}" for f in fs]
        lines.append('')

if args.symbols == 'success':
    lines += ['### TypeScript public API', '']
    if not sym:
        lines += [f'_No public API changes in {pkg_names}. Implementation changes are not visible to this check._', '']
    for (pkg, symbol), fs in group(sym):
        top = min(fs, key=lambda f: ORDER[f['level']])
        head = f"**`{pkg}` › `{symbol}`** ({top['kind'] if top['symbol'] == top['target'] else 'members'})"
        if top['level'] in ('ERR', 'WARN') and pkg in packages:
            used = usages(symbol, packages[pkg])
            if used:
                dirs = sorted({package_of(u) for u in used})
                shown = ', '.join(f'`{d}`' for d in dirs[:4]) + (f' and {len(dirs) - 4} more' if len(dirs) > 4 else '')
                head += f' · used in {len(used)} file(s) across {len(dirs)} package(s): {shown}'
            else:
                head += ' · no usages elsewhere in this repo'
        lines.append(head)
        for f in fs:
            prefix = '' if f['target'] == f['symbol'] else f"`{f['target']}`: "
            lines.append(f"- {ICON[f['level']]} `{f['rule']}`: {prefix}{f['text']}")
        lines.append('')
    if args.diffs and os.path.isdir(args.diffs):
        for name in sorted(os.listdir(args.diffs)):
            with open(os.path.join(args.diffs, name)) as fh:
                body = fh.read().strip()
            if not body:
                continue
            if len(body) > 6000:
                body = body[:6000] + '\n… (truncated; see the workflow artifact)'
            lines += [f'<details><summary>API report diff: <code>{name.removesuffix(".diff")}</code></summary>', '',
                      '```diff', body, '```', '', '</details>', '']

notes = ['<sub>']
notes.append('ERR fails the check. WARN and INFO are reported only. ')
if args.symbols == 'success':
    notes.append('TypeScript findings compare declaration text, so type changes are WARN; '
                 '"used in" is a name match across the monorepo, not a resolved reference. ')
if args.api == 'success' and api:
    notes.append('HTTP consumers are not known yet; that needs the contract graph. ')
notes.append('</sub>')
lines.append(''.join(notes))

with open(args.out, 'w') as f:
    f.write('\n'.join(lines) + '\n')
with open(args.verdict, 'w') as f:
    f.write(str(total['ERR'] + len(failed)))
