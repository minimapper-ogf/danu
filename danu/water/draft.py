"""Replaceable JOSM drafts, kept in terrain squares, never in the map API.

Each category is a separate source. All copied primitives get fresh local
IDs and an ownership tag, so even two JOSM layers starting at -1 are safe.
A non-geographic relation records the source in each affected square, even
when the source is empty. It travels through save and autosave like the data.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field, replace
from hashlib import sha256
from itertools import pairwise
from pathlib import Path
from xml.etree import ElementTree as ET

from ..core import edits
from ..core.rings import closed_rings
from ..core.square import Member, Relation, open_square_file
from . import overpass, peaks

OWNER = 'danu:draft'
SOURCE = 'danu:draft:source'
FILENAME = 'danu:draft:filename'
CATEGORIES = {'water': 'water features', 'coastline': 'coastline', 'peaks': 'peaks'}


@dataclass
class Draft:
    data: overpass.Water = field(default_factory=overpass.Water)
    roots: list[tuple[str, int]] = field(default_factory=list)
    broken: list[str] = field(default_factory=list)
    outside: int = 0


def _tables(data):
    return {'node': data.nodes, 'way': data.ways, 'relation': data.relations}


def _dependencies(data, kind, fid):
    """Existing geometry reached by an outside reference, even in a damaged square."""
    todo, found = [(kind, fid)], set()
    while todo:
        key = todo.pop()
        if key in found:
            continue
        kind, fid = key
        table = _tables(data).get(kind, {})
        if fid not in table:
            continue
        found.add(key)
        obj = table[fid]
        if kind == 'way':
            todo.extend(('node', i) for i in obj.refs)
        elif kind == 'relation':
            todo.extend((m.type, m.ref) for m in obj.members)
    return found


def _closure(data, kind, fid, visiting=None, found=None):
    visiting = set() if visiting is None else visiting
    found = set() if found is None else found
    key = kind, fid
    if key in visiting:
        raise ValueError('cyclic relation membership')
    if key in found:
        return found
    table = _tables(data).get(kind)
    if table is None or fid not in table:
        raise ValueError(f'missing {kind} {fid}')
    obj = table[fid]
    visiting.add(key)
    if kind == 'way':
        if len(obj.refs) < 2:
            raise ValueError('way has fewer than two nodes')
        for nid in obj.refs:
            _closure(data, 'node', nid, visiting, found)
    elif kind == 'relation':
        if not obj.members:
            raise ValueError('relation has no members')
        for mem in obj.members:
            _closure(data, mem.type, mem.ref, visiting, found)
        if obj.tags.get('type') == 'multipolygon':
            for role in ('outer', 'inner'):
                pieces = [data.ways[m.ref].refs for m in obj.members
                          if m.type == 'way' and (m.role or 'outer') == role]
                rings = closed_rings(pieces)
                if sum(len(p) - 1 for p in pieces) != sum(len(r) - 1 for r in rings):
                    raise ValueError(f'unclosed {role} ring')
            if not any(m.type == 'relation' or (m.type == 'way' and m.role in ('', 'outer'))
                       for m in obj.members):
                raise ValueError('multipolygon has no outer ring')
    visiting.remove(key)
    found.add(key)
    return found


def _segment_in(a, b, bounds):
    """Segment/rectangle intersection, including segments with both ends out."""
    west, south, east, north = bounds
    lo, hi = 0.0, 1.0
    for start, end, lower, upper in ((a.lon, b.lon, west, east), (a.lat, b.lat, south, north)):
        delta = end - start
        if delta == 0:
            if not lower <= start <= upper:
                return False
            continue
        t0, t1 = sorted(((lower - start) / delta, (upper - start) / delta))
        lo, hi = max(lo, t0), min(hi, t1)
        if lo > hi:
            return False
    return True


def _inside_ring(lon, lat, refs, nodes):
    inside = False
    for i, j in pairwise(refs):
        a, b = nodes[i], nodes[j]
        if (a.lat > lat) != (b.lat > lat) and lon < (b.lon - a.lon) * (lat - a.lat) / (b.lat - a.lat) + a.lon:
            inside = not inside
    return inside


def _intersects(data, closure, name):
    nodes = [data.nodes[i] for k, i in closure if k == 'node']
    if any(name.contains(n.lon, n.lat) for n in nodes):
        return True
    ways = [data.ways[i] for k, i in closure if k == 'way']
    if any(_segment_in(data.nodes[a], data.nodes[b], name.bounds)
           for way in ways for a, b in pairwise(way.refs)):
        return True
    # The entire square may be inside a lake, without a vertex or edge in it.
    rings = closed_rings([way.refs for way in ways])
    return sum(_inside_ring(name.lon + .5, name.lat + .5, r, data.nodes) for r in rings) % 2 == 1


def read(path: str | Path, category: str) -> Draft:
    if category not in CATEGORIES:
        raise ValueError(f'unknown draft category: {category}')
    with open_square_file(path) as f:
        root = ET.parse(f).getroot()
    if root.tag != 'osm':
        raise ValueError('expected an .osm file saved from JOSM')
    # JOSM keeps deleted primitives in its saved layer; they are not geometry.
    for elem in list(root):
        if elem.get('action') == 'delete' or elem.get('visible') == 'false':
            root.remove(elem)
    data = overpass.parse(ET.tostring(root))
    out = Draft(data=data)
    for node in data.nodes.values():
        if not (math.isfinite(node.lon) and math.isfinite(node.lat)
                and -180 <= node.lon <= 180 and -90 <= node.lat <= 90):
            raise ValueError(f'node {node.id}: invalid coordinates')
    tables = _tables(data)
    for kind, table in tables.items():
        for fid, obj in table.items():
            tags = obj.tags
            selected = ((category == 'water' and overpass.asked_for(kind, tags))
                        or (category == 'coastline' and kind == 'way' and tags.get('natural') == 'coastline')
                        or (category == 'peaks' and kind == 'node' and tags.get('natural') in peaks.KINDS))
            if not selected:
                continue
            try:
                _closure(data, kind, fid)
                if category == 'water' and kind == 'way' and (
                        tags.get('natural') == 'water' or tags.get('waterway') in overpass.AREA_KINDS):
                    if not obj.closed or len(obj.refs) < 4:
                        raise ValueError('water area is not a closed ring')
                if category == 'water' and kind == 'relation' and tags.get('type') != 'multipolygon':
                    raise ValueError('water relation is not a multipolygon')
                if category == 'peaks':
                    height = peaks.metres(tags.get('ele'))
                    if height is None or not math.isfinite(height):
                        raise ValueError('peak has no usable elevation')
                    tags['ele'] = peaks.format_ele(height)
                if category == 'coastline':
                    tags['ele'] = '0'
            except ValueError as exc:
                out.broken.append(f'{kind} {fid}: {exc}')
            else:
                out.roots.append((kind, fid))
    # A selected member of a broken relation must not leak in as a fragment.
    broken_relations = {int(s.split(':', 1)[0].split()[1]) for s in out.broken if s.startswith('relation ')}
    blocked = set()
    def block(rid, seen):
        if rid in seen or rid not in data.relations:
            return
        seen.add(rid)
        for mem in data.relations[rid].members:
            blocked.add((mem.type, mem.ref))
            if mem.type == 'relation':
                block(mem.ref, seen)
    for rid in broken_relations:
        block(rid, set())
    out.roots = [r for r in out.roots if r not in blocked]
    return out


def sources(working_set, category):
    return sorted({r.tags[SOURCE] for sq in working_set.squares.values() for r in sq.relations.values()
                   if r.tags.get(OWNER) == category and SOURCE in r.tags})


def source_key(path):
    # The local absolute path belongs in local settings, not a shared square.
    return sha256(str(Path(path).resolve()).encode()).hexdigest()


def source_names(working_set, category):
    return sorted({r.tags.get(FILENAME, 'Draft file') for sq in working_set.squares.values()
                   for r in sq.relations.values() if r.tags.get(OWNER) == category and SOURCE in r.tags})


def place(draft: Draft, names) -> dict:
    """Only roots intersecting the working squares, each with its whole geometry."""
    placed = {}
    draft.outside = 0
    for kind, fid in draft.roots:
        closure = _closure(draft.data, kind, fid)
        hits = [n for n in sorted(names) if _intersects(draft.data, closure, n)]
        if not hits:
            draft.outside += 1
            continue
        counts = Counter(n for k, i in closure if k == 'node' for n in hits
                         if n.contains(draft.data.nodes[i].lon, draft.data.nodes[i].lat))
        home = max(hits, key=lambda n: counts[n])
        got = placed.setdefault(home, overpass.Water())
        for k, i in closure:
            _tables(got)[k][i] = _tables(draft.data)[k][i]
    return placed


@dataclass
class ReplaceDraft(edits.Command):
    """A source replacement, with exact inverses and journal coverage."""
    category: str
    before: overpass.Water
    after: overpass.Water
    order: dict | None = field(default=None, repr=False)

    def apply(self, square):
        if self.order is None:
            self.order = {k: tuple(t) for k, t in _tables(square).items()}
        self._swap(square, self.before, self.after)

    def undo(self, square):
        self._swap(square, self.after, self.before)
        # Order affects raster burning, and is part of an exact undo.
        for kind, order in self.order.items():
            table = _tables(square)[kind]
            ordered = {i: table[i] for i in order}
            table.clear()
            table.update(ordered)

    @staticmethod
    def _swap(square, old, new):
        for kind, table in _tables(old).items():
            target = _tables(square)[kind]
            for fid in table:
                target.pop(fid, None)
            target.update(_tables(new)[kind])

    def describe(self):
        return f'replace draft {CATEGORIES[self.category]}'

    def ways(self, square):
        return set(self.before.ways) | set(self.after.ways)

    def spots(self, square):
        return {i for data in (self.before, self.after) for i, n in data.nodes.items() if 'ele' in n.tags}

    def touched(self, square):
        return tuple(set(_tables(self.before)[k]) | set(_tables(self.after)[k])
                     for k in ('node', 'way', 'relation'))

    def taken(self, square):
        return tuple({i: obj for i, obj in _tables(self.before)[k].items() if i not in _tables(self.after)[k]}
                     for k in ('node', 'way', 'relation'))


def commands(working_set, category, placed, source, alloc):
    """Replace just this source; preserve geometry adopted by local drawings."""
    for square in working_set.squares.values():
        alloc.include(square)
    steps = []
    for name, square in working_set.squares.items():
        before, after = overpass.Water(), overpass.Water()
        for kind, table in _tables(square).items():
            _tables(before)[kind].update({i: obj for i, obj in table.items() if obj.tags.get(OWNER) == category})
        # Retain dependencies adopted by non-draft features, stripping ownership.
        retained = set()
        for wid, way in square.ways.items():
            if wid not in before.ways:
                retained.update(('node', n) for n in way.refs)
        for rid, rel in square.relations.items():
            if rid not in before.relations:
                for mem in rel.members:
                    retained |= _dependencies(square, mem.type, mem.ref)
        for kind, fid in retained:
            if fid in _tables(before)[kind]:
                obj = _tables(before)[kind][fid]
                _tables(after)[kind][fid] = replace(obj, tags={k: v for k, v in obj.tags.items()
                                                              if k not in (OWNER, SOURCE, FILENAME)})
        got = placed.get(name, overpass.Water())
        ids = {(k, i): alloc.take() for k, table in _tables(got).items() for i in table}
        for kind, table in _tables(got).items():
            for fid, obj in table.items():
                tags = dict(obj.tags)
                if kind == 'node' and category != 'peaks':
                    tags = {}
                elif kind == 'way' and category == 'water' and not overpass.asked_for(kind, tags):
                    tags = {}
                tags[OWNER] = category
                copied = replace(obj, id=ids[kind, fid], version=None, tags=tags)
                if kind == 'way':
                    copied.refs = [ids['node', i] for i in obj.refs]
                elif kind == 'relation':
                    copied.members = [Member(m.type, ids[m.type, m.ref], m.role) for m in obj.members]
                _tables(after)[kind][copied.id] = copied
        if source is not None:
            rid = alloc.take()
            after.relations[rid] = Relation(rid, tags={OWNER: category, SOURCE: source_key(source),
                                                     FILENAME: Path(source).name})
        if any(_tables(before).values()) or any(_tables(after).values()):
            steps.append((square, ReplaceDraft(category, before, after)))
    return steps
