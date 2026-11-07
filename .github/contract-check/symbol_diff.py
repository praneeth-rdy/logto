"""Classify public TypeScript API changes between two api-extractor doc models.

api-extractor only reports *that* a package's API report changed. This script
compares the base and head `*.api.json` models and gives each change a level,
mirroring oasdiff:

  ERR  = breaking for consumers (export removed, required parameter added, ...)
  WARN = possibly breaking; needs a human or a type checker (type text changed, ...)
  INFO = additive (export added, optional parameter added, ...)

Type compatibility is judged on declaration text, not by the TypeScript checker, so
any type change is WARN: `string` -> `string | number` cannot be told apart from
`string` -> `number` here.

Usage: symbol_diff.py <package-name> <base.api.json|-> <head.api.json|-> > findings.json
"""
import json
import re
import sys

FUNCTION_KINDS = {'Function', 'Method', 'MethodSignature', 'Constructor', 'ConstructSignature', 'CallSignature'}
CONTAINER_KINDS = {'Interface', 'Class', 'Namespace', 'Enum'}
TYPE_RANGE_KEYS = ('variableTypeTokenRange', 'propertyTypeTokenRange', 'typeTokenRange', 'initializerTokenRange')
OPEN, CLOSE = '([{<', ')]}>'
KIND_LABEL = {'PropertySignature': 'property', 'MethodSignature': 'method', 'TypeAlias': 'type', 'Variable': 'const',
              'EnumMember': 'enum member', 'CallSignature': 'call signature', 'ConstructSignature': 'construct signature'}


def label(kind):
    return KIND_LABEL.get(kind, kind.lower())


# --- loading ---------------------------------------------------------------------

def load(path):
    if path == '-':
        return {}
    with open(path) as f:
        model = json.load(f)
    items = {}

    def walk(node, parent):
        for m in node.get('members', []):
            if m['kind'] == 'EntryPoint':
                walk(m, None)
                continue
            ref = m.get('canonicalReference') or f"{parent}#{m.get('name')}"
            m['_parent'] = parent
            items[ref] = m
            if m['kind'] in CONTAINER_KINDS:
                walk(m, ref)

    walk(model, None)
    return items


# --- text helpers ----------------------------------------------------------------

def norm(text):
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    text = re.sub(r'//[^\n]*', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'^(export )?(declare )?', '', text)
    return text.rstrip(';').strip()


def excerpt(m, key=None):
    tokens = m.get('excerptTokens', [])
    if key:
        r = m.get(key)
        if not r:
            return None
        tokens = tokens[r['startIndex']:r['endIndex']]
    return norm(''.join(t['text'] for t in tokens))


def split_top(text, seps):
    """Split on separators that are not nested in brackets or strings."""
    parts, depth, cur, quote, i = [], 0, '', None, 0
    while i < len(text):
        c = text[i]
        if quote:
            cur += c
            if c == quote and text[i - 1] != '\\':
                quote = None
        elif c in '"\'`':
            quote = c
            cur += c
        elif text.startswith('=>', i):
            cur += '=>'
            i += 2
            continue
        elif c in OPEN:
            depth += 1
            cur += c
        elif c in CLOSE:
            depth -= 1
            cur += c
        elif depth == 0 and c in seps:
            parts.append(cur.strip())
            cur = ''
        else:
            cur += c
        i += 1
    if cur.strip():
        parts.append(cur.strip())
    return parts


def match_close(text, start):
    depth = 0
    for i in range(start, len(text)):
        if text.startswith('=>', i):
            continue
        if text[i] in OPEN:
            depth += 1
        elif text[i] in CLOSE and not (text[i] == '>' and i and text[i - 1] == '='):
            depth -= 1
            if depth == 0:
                return i
    return -1


def parse_param(p):
    head = split_top(p, ':')
    name = head[0].strip()
    type_ = p[len(head[0]) + 1:].strip() if len(head) > 1 else 'any'
    optional = name.endswith('?') or name.startswith('...')
    name = name.rstrip('?').strip()
    if name.startswith('{') or name.startswith('['):
        name = '(destructured)'
    return {'name': name, 'optional': optional, 'type': norm(type_)}


def parse_arrow(text):
    """`<T>(a: X, b?: Y) => Z` -> ([params], 'Z'), or None if not a function type."""
    if not text:
        return None
    i = 0
    if text.startswith('<'):
        i = match_close(text, 0) + 1
        if i <= 0:
            return None
    text_from = text[i:].lstrip()
    if not text_from.startswith('('):
        return None
    close = match_close(text_from, 0)
    if close < 0 or not text_from[close + 1:].lstrip().startswith('=>'):
        return None
    params = [parse_param(p) for p in split_top(text_from[1:close], ',')]
    ret = text_from[close + 1:].lstrip()[2:].strip()
    return params, norm(ret)


def parse_object(text):
    """`{ a: X; b?: Y }` -> {'a': {...}, 'b': {...}}, or None if not an object literal."""
    if not text or not text.startswith('{') or match_close(text, 0) != len(text) - 1:
        return None
    members = {}
    for part in split_top(text[1:-1], ';,'):
        if not part:
            continue
        part = re.sub(r'^readonly ', '', part)
        head = re.split(r'[:(]', part, maxsplit=1)[0].strip()
        optional = head.endswith('?')
        name = head.rstrip('?').strip()
        type_ = part[len(head):].lstrip('?').lstrip(':').strip()
        members[name] = {'optional': optional, 'type': norm(type_)}
    return members


def fn_shape(m):
    if m['kind'] in FUNCTION_KINDS and 'parameters' in m:
        params = [{
            'name': p['parameterName'],
            'optional': bool(p.get('isOptional')),
            'type': excerpt({**m, 'r': p['parameterTypeTokenRange']}, 'r'),
        } for p in m['parameters']]
        return params, excerpt(m, 'returnTypeTokenRange')
    for key in TYPE_RANGE_KEYS:
        shape = parse_arrow(excerpt(m, key))
        if shape:
            return shape
    return None


def type_text(m):
    for key in TYPE_RANGE_KEYS:
        t = excerpt(m, key)
        if t is not None:
            return t
    return None


def deprecated(m):
    return '@deprecated' in (m.get('docComment') or '')


# --- rules -----------------------------------------------------------------------

def compare_params(old, new, emit):
    old_params, old_ret = old
    new_params, new_ret = new
    for i, p in enumerate(new_params):
        if i >= len(old_params):
            if p['optional']:
                emit('INFO', 'optional-parameter-added', f"added the optional parameter `{p['name']}`")
            else:
                emit('ERR', 'required-parameter-added', f"added the required parameter `{p['name']}`")
            continue
        o = old_params[i]
        label = f"`{p['name']}`" if p['name'] == o['name'] else f"`{o['name']}` (now `{p['name']}`)"
        if o['optional'] and not p['optional']:
            emit('ERR', 'parameter-became-required', f"the parameter {label} became required")
        elif not o['optional'] and p['optional']:
            emit('INFO', 'parameter-became-optional', f"the parameter {label} became optional")
        if o['type'] != p['type']:
            emit('WARN', 'parameter-type-changed', f"the type of parameter {label} changed from `{o['type']}` to `{p['type']}`")
    for o in old_params[len(new_params):]:
        emit('ERR', 'parameter-removed', f"removed the parameter `{o['name']}`; callers that still pass it no longer compile")
    if old_ret != new_ret:
        emit('WARN', 'return-type-changed', f"the return type changed from `{old_ret}` to `{new_ret}`")


def compare_objects(old, new, emit):
    for name, o in old.items():
        if name not in new:
            emit('ERR', 'property-removed', f"removed the property `{name}`")
    for name, p in new.items():
        o = old.get(name)
        if o is None:
            if p['optional']:
                emit('INFO', 'optional-property-added', f"added the optional property `{name}`")
            else:
                emit('WARN', 'required-property-added',
                     f"added the required property `{name}`; code that creates this type must now set it")
            continue
        if o['optional'] != p['optional']:
            became = 'optional' if p['optional'] else 'required'
            emit('WARN', f'property-became-{became}',
                 f"the property `{name}` became {became}; breaks {'readers' if p['optional'] else 'code that creates this type'}")
        if o['type'] != p['type']:
            emit('WARN', 'property-type-changed', f"the type of `{name}` changed from `{o['type']}` to `{p['type']}`")


def diff(package, base, head):
    findings = []

    def finding(m, level, rule, text):
        name = m.get('name') or '(anonymous)'
        parent = m.get('_parent')
        target = f"{parent.split('!')[-1].split(':')[0]}.{name}" if parent else name
        findings.append({
            'tool': 'api-extractor', 'package': package, 'level': level, 'rule': rule,
            'target': target, 'symbol': (parent or m.get('canonicalReference', name)).split('!')[-1].split(':')[0].split('#')[0],
            'kind': label(m['kind']), 'text': text, 'location': m.get('fileUrlPath'),
        })

    for ref, o in base.items():
        if ref in head or (o.get('_parent') and o['_parent'] not in head):
            continue
        rule = 'member-removed' if o.get('_parent') else 'export-removed'
        finding(o, 'ERR', rule, f"removed the {label(o['kind'])} `{o.get('name')}`"
                + ('' if deprecated(o) else ' without deprecating it first'))

    for ref, n in head.items():
        o = base.get(ref)
        if o is None:
            if n.get('_parent') and n['_parent'] not in base:
                continue
            if n['kind'] == 'PropertySignature' and not n.get('isOptional'):
                finding(n, 'WARN', 'required-property-added',
                        f"added the required property `{n.get('name')}`; code that creates this type must now set it")
            else:
                finding(n, 'INFO', 'member-added' if n.get('_parent') else 'export-added',
                        f"added the {label(n['kind'])} `{n.get('name')}`")
            continue

        emit = lambda level, rule, text, n=n: finding(n, level, rule, text)
        if not deprecated(o) and deprecated(n):
            emit('INFO', 'deprecated', 'marked as deprecated')
        if o['kind'] != n['kind']:
            emit('ERR', 'kind-changed', f"changed from a {label(o['kind'])} to a {label(n['kind'])}")
            continue
        if excerpt(o) == excerpt(n) and o.get('isOptional') == n.get('isOptional'):
            continue  # only docs changed, or nothing

        before = len(findings)
        if o.get('isOptional') is not None and o.get('isOptional') != n.get('isOptional'):
            became = 'optional' if n.get('isOptional') else 'required'
            emit('WARN', f'property-became-{became}', f'the property became {became}')
        o_fn, n_fn = fn_shape(o), fn_shape(n)
        o_obj, n_obj = parse_object(type_text(o)), parse_object(type_text(n))
        if o_fn and n_fn:
            compare_params(o_fn, n_fn, emit)
        elif o_obj is not None and n_obj is not None:
            compare_objects(o_obj, n_obj, emit)
        elif n['kind'] not in CONTAINER_KINDS and type_text(o) != type_text(n):
            emit('WARN', 'type-changed', f"the type changed from `{type_text(o)}` to `{type_text(n)}`")
        if len(findings) == before and n['kind'] not in CONTAINER_KINDS:
            emit('WARN', 'signature-changed', f"the declaration changed: `{excerpt(o)}` → `{excerpt(n)}`")

    order = {'ERR': 0, 'WARN': 1, 'INFO': 2}
    return sorted(findings, key=lambda f: (order[f['level']], f['symbol'], f['target']))


if __name__ == '__main__':
    pkg, base_path, head_path = sys.argv[1:4]
    json.dump(diff(pkg, load(base_path), load(head_path)), sys.stdout, indent=2)
    print()
