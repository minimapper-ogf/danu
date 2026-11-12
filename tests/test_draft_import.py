"""JOSM draft replacement, identity, filtering, save and exact undo."""

import lzma
from copy import deepcopy

import pytest

from danu.core import edits
from danu.core.square import (
    Node,
    Square,
    SquareName,
    Way,
    WorkingSet,
    read_square,
    write_square,
)
from danu.water import draft

HERE, EAST = SquareName(10, 10), SquareName(11, 10)


def file(tmp_path, body, name='draft.osm'):
    path = tmp_path / name
    path.write_text(f'<osm version="0.6">{body}</osm>')
    return path


def river(lon=10.2, fid=-1):
    return (f'<node id="-1" lon="{lon}" lat="10.2"/>'
            f'<node id="-2" lon="{lon + .2}" lat="10.4"/>'
            f'<way id="{fid}"><nd ref="-1"/><nd ref="-2"/>'
            '<tag k="waterway" v="river"/></way>')


def working_set():
    return WorkingSet(HERE, 1, {HERE: Square(HERE), EAST: Square(EAST)})


def load(ws, history, path, category):
    result = draft.read(path, category)
    placed = draft.place(result, ws.squares)
    history.do_across(draft.commands(ws, category, placed, path, history.alloc(ws.squares[HERE])))
    return result


def test_new_josm_ids_never_collide_and_replacement_is_exactly_undoable(tmp_path):
    ws, history = working_set(), edits.SetUndoStack()
    sq = ws.squares[HERE]
    sq.nodes[-1] = Node(-1, 10.6, 10.6)
    sq.nodes[-2] = Node(-2, 10.7, 10.7)
    sq.ways[-1] = Way(-1, [-1, -2], {'ele': '100'})
    original = deepcopy(ws.squares)
    path = file(tmp_path, river())
    load(ws, history, path, 'water')
    assert sq.ways[-1] == original[HERE].ways[-1]
    first = deepcopy(ws.squares)
    imported = [w for w in sq.ways.values() if draft.OWNER in w.tags]
    assert len(imported) == 1 and imported[0].id < -2
    assert imported[0].refs != [-1, -2]
    path.write_text('<osm version="0.6">' + river(11.2, -900) + '</osm>')
    load(ws, history, path, 'water')
    second = deepcopy(ws.squares)
    assert list(sq.ways) == [-1]
    assert len(ws.squares[EAST].ways) == 1
    history.undo_across()
    assert ws.squares == first
    history.redo_across()
    assert ws.squares == second
    history.undo_across()
    history.undo_across()
    assert ws.squares == original


def test_categories_are_separate_and_sources_survive_save(tmp_path):
    ws, history = working_set(), edits.SetUndoStack()
    path = file(tmp_path, river() + '<node id="-3" lon="10.5" lat="10.5">'
                '<tag k="natural" v="peak"/><tag k="ele" v="750"/></node>')
    load(ws, history, path, 'water')
    load(ws, history, path, 'peaks')
    sq = ws.squares[HERE]
    assert len(sq.ways) == 1
    assert [n.tags.get('ele') for n in sq.nodes.values() if n.tags.get('natural') == 'peak'] == ['750']
    saved = tmp_path / f'{HERE}.osm.xz'
    write_square(sq, saved)
    ws.squares[HERE] = read_square(saved)
    assert draft.sources(ws, 'water') == [draft.source_key(path)]
    assert draft.sources(ws, 'peaks') == [draft.source_key(path)]
    assert read_square(saved).attrs['upload'] == 'never'
    assert str(tmp_path) not in lzma.open(saved, 'rt').read()
    # A renamed source is an explicit replacement, not a second batch.
    empty = file(tmp_path, '', 'renamed.osm')
    load(ws, history, empty, 'water')
    assert not ws.squares[HERE].ways
    assert any(n.tags.get('natural') == 'peak' for n in ws.squares[HERE].nodes.values())
    assert draft.sources(ws, 'water') == [draft.source_key(empty)]


def test_filters_unrelated_features_deleted_objects_and_outside_peaks(tmp_path):
    ws, history = working_set(), edits.SetUndoStack()
    body = river(30) + '<way id="-5"><nd ref="-1"/><nd ref="-2"/><tag k="highway" v="primary"/></way>'
    body += '<node id="-3" lon="10.5" lat="10.5"><tag k="natural" v="peak"/><tag k="ele" v="800 ft"/></node>'
    body += '<node id="-4" lon="30.5" lat="10.5"><tag k="natural" v="peak"/><tag k="ele" v="900"/></node>'
    body += '<node id="-99" action="delete"/>'
    body += '<way id="-99" action="delete"><tag k="natural" v="coastline"/></way>'
    path = file(tmp_path, body)
    assert load(ws, history, path, 'water').outside == 1
    assert not any(sq.ways for sq in ws.squares.values())
    assert load(ws, history, path, 'peaks').outside == 1
    # A blank square with no contours accepts a draft peak.
    assert [n.tags['ele'] for n in ws.squares[HERE].nodes.values() if 'ele' in n.tags] == ['244']
    assert not draft.read(path, 'coastline').roots


def test_coastline_at_zero_and_whole_geometry_crossing_with_no_inside_nodes(tmp_path):
    ws, history = working_set(), edits.SetUndoStack()
    body = ('<node id="-1" lon="9" lat="10.5"/><node id="-2" lon="13" lat="10.5"/>'
            '<way id="-1"><nd ref="-1"/><nd ref="-2"/><tag k="natural" v="coastline"/></way>')
    result = load(ws, history, file(tmp_path, body), 'coastline')
    assert not result.outside
    ways = [w for sq in ws.squares.values() for w in sq.ways.values()]
    assert len(ways) == 1 and ways[0].ele == 0
    sq = next(sq for sq in ws.squares.values() if sq.ways)
    assert [sq.nodes[i].lon for i in ways[0].refs] == [9, 13]


def test_incomplete_features_reported_and_not_imported_as_fragments(tmp_path):
    body = river() + '<way id="-2"><nd ref="-1"/><nd ref="-999"/><tag k="natural" v="coastline"/></way>'
    body += '<relation id="-1"><member type="way" ref="-1" role="outer"/>'
    body += '<member type="way" ref="-999" role="inner"/><tag k="type" v="multipolygon"/><tag k="natural" v="water"/></relation>'
    path = file(tmp_path, body)
    water = draft.read(path, 'water')
    assert water.broken == ['relation -1: missing way -999']
    assert not water.roots
    coast = draft.read(path, 'coastline')
    assert coast.broken == ['way -2: missing node -999']
    assert not coast.roots


def test_unclosed_multipolygon_reported(tmp_path):
    body = river() + '<relation id="-1"><member type="way" ref="-1" role="outer"/>'
    body += '<tag k="type" v="multipolygon"/><tag k="natural" v="water"/></relation>'
    got = draft.read(file(tmp_path, body), 'water')
    assert got.broken == ['relation -1: unclosed outer ring']
    assert not got.roots


def test_lake_surrounding_working_area_is_imported(tmp_path):
    body = ''.join(f'<node id="{i}" lon="{x}" lat="{y}"/>'
                   for i, (x, y) in enumerate(((9, 9), (13, 9), (13, 12), (9, 12)), 1))
    body += '<way id="1"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/><tag k="natural" v="water"/></way>'
    ws, history = working_set(), edits.SetUndoStack()
    assert load(ws, history, file(tmp_path, body), 'water').outside == 0
    assert sum(len(sq.ways) for sq in ws.squares.values()) == 1


def test_complete_multipolygon_keeps_islands_and_remaps_every_member(tmp_path):
    body = ''
    for base, coords in ((1, ((10.1, 10.1), (11.5, 10.1), (11.5, 10.8), (10.1, 10.8))),
                         (5, ((10.3, 10.3), (10.5, 10.3), (10.5, 10.5), (10.3, 10.5)))):
        body += ''.join(f'<node id="{-i}" lon="{x}" lat="{y}"/>'
                        for i, (x, y) in enumerate(coords, base))
        body += f'<way id="{-base}">' + ''.join(f'<nd ref="{-i}"/>' for i in (*range(base, base + 4), base)) + '</way>'
    body += ('<relation id="-1"><member type="way" ref="-1" role="outer"/>'
             '<member type="way" ref="-5" role="inner"/><tag k="natural" v="water"/>'
             '<tag k="water" v="lake"/><tag k="type" v="multipolygon"/></relation>')
    ws, history = working_set(), edits.SetUndoStack()
    assert not load(ws, history, file(tmp_path, body), 'water').broken
    rel = next(r for sq in ws.squares.values() for r in sq.relations.values() if r.tags.get('natural') == 'water')
    assert [m.role for m in rel.members] == ['outer', 'inner']
    sq = next(sq for sq in ws.squares.values() if rel.id in sq.relations)
    assert len(sq.ways) == 2 and len(sq.nodes) == 8
    for member in rel.members:
        assert member.ref in sq.ways
        assert all(i in sq.nodes for i in sq.ways[member.ref].refs)


def test_removal_preserves_nodes_adopted_by_a_contour_and_undo_order(tmp_path):
    ws, history = working_set(), edits.SetUndoStack()
    path = file(tmp_path, river())
    load(ws, history, path, 'water')
    sq = ws.squares[HERE]
    way = next(iter(sq.ways.values()))
    refs = way.refs[:]
    history.do(sq, edits.AddWay(-9999, [-9998, -9997], [(10.5, 10.5), (10.6, 10.6)], {'ele': '100'}))
    history.do(sq, edits.ExtendWayWithExisting(-9999, True, refs[0]))
    before = deepcopy(sq)
    history.do_across(draft.commands(ws, 'water', {}, None, history.alloc(sq)))
    assert -9999 in sq.ways and way.id not in sq.ways
    assert refs[0] in sq.nodes and draft.OWNER not in sq.nodes[refs[0]].tags
    assert refs[1] not in sq.nodes
    history.undo_across()
    assert sq == before
    assert list(sq.ways) == list(before.ways)
    assert list(sq.nodes) == list(before.nodes)


def test_bad_file_refused_before_any_replacement(tmp_path):
    path = file(tmp_path, '<node')
    with pytest.raises(Exception, match='not well-formed'):
        draft.read(path, 'water')
    path = file(tmp_path, '')
    path.write_text('<osmChange version="0.6"/>')
    with pytest.raises(ValueError, match='expected an .osm'):
        draft.read(path, 'water')
