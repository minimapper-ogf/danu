"""Draft menu operations, failure atomicity, and clipping of complete geometry."""

from copy import deepcopy

import pytest

pytest.importorskip('PySide6')

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QMessageBox, QStyleOptionGraphicsItem

from danu.core.square import Node, Square, SquareName, Way, WorkingSet
from danu.ui import mercator as m
from danu.ui.contours import ContourLayer
from danu.water import draft


def source(tmp_path, coast=False):
    path = tmp_path / 'josm.osm'
    path.write_text('<osm version="0.6"><node id="-1" lon="125.4" lat="-22.7"/>'
                    '<node id="-2" lon="125.6" lat="-22.6"/>'
                    '<way id="-1"><nd ref="-1"/><nd ref="-2"/>'
                    + ('<tag k="natural" v="coastline"/>' if coast else '<tag k="waterway" v="river"/>')
                    + '</way></osm>')
    return path


def imported_ways(window, category):
    return [w for sq in window.working_set.squares.values() for w in sq.ways.values()
            if w.tags.get(draft.OWNER) == category]


def test_import_reload_remove_and_undo_refresh_the_canvas(window, tmp_path):
    w = window
    w.draft_imports.runner = lambda job: job.run()
    path = source(tmp_path, coast=True)
    before = deepcopy(w.working_set.squares)
    assert w.draft_imports.import_file('coastline', path)
    first = deepcopy(w.working_set.squares)
    assert len(imported_ways(w, 'coastline')) == 1
    way = imported_ways(w, 'coastline')[0]
    assert (SquareName(125, -23), way.id) in w.contours.coasts
    path.write_text('<osm version="0.6"/>')
    assert w.draft_imports.reload('coastline')
    assert not imported_ways(w, 'coastline')
    assert (SquareName(125, -23), way.id) not in w.contours.coasts
    w.editor.undo()
    assert w.working_set.squares == first
    assert w.draft_imports.remove('coastline')
    assert not draft.sources(w.working_set, 'coastline')
    w.editor.undo()
    w.editor.undo()
    assert w.working_set.squares == before


def test_broken_geometry_can_be_cancelled_or_skipped(window, tmp_path, monkeypatch):
    w = window
    w.draft_imports.runner = lambda job: job.run()
    path = source(tmp_path)
    w.draft_imports.import_file('water', path)
    before = deepcopy(w.working_set.squares)
    path.write_text(path.read_text().replace('</osm>', '<way id="-9"><nd ref="-999"/>'
                                           '<nd ref="-1"/><tag k="waterway" v="river"/></way></osm>'))
    reports = []
    monkeypatch.setattr(w.draft_imports, 'accept_valid', lambda broken: reports.extend(broken) or False)
    w.draft_imports.reload('water')
    assert reports == ['way -9: missing node -999']
    assert w.working_set.squares == before
    monkeypatch.setattr(w.draft_imports, 'accept_valid', lambda broken: True)
    w.draft_imports.reload('water')
    assert len(imported_ways(w, 'water')) == 1
    assert '1 broken' in w.statusBar().currentMessage()


def test_missing_or_malformed_file_keeps_previous_import(window, tmp_path, monkeypatch):
    w = window
    w.draft_imports.runner = lambda job: job.run()
    path = source(tmp_path)
    w.draft_imports.import_file('water', path)
    before = deepcopy(w.working_set.squares)
    messages = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: messages.append(args[2]))
    path.write_text('not XML')
    w.draft_imports.reload('water')
    path.unlink()
    w.draft_imports.reload('water')
    assert len(messages) == 2
    assert w.working_set.squares == before


def test_late_answer_cannot_replace_a_removed_source_or_another_working_area(window, tmp_path):
    w = window
    jobs = []
    w.draft_imports.runner = jobs.append
    w.draft_imports.import_file('water', source(tmp_path))
    w.draft_imports.remove('water')
    jobs.pop().run()
    assert not imported_ways(w, 'water')
    w.draft_imports.import_file('water', source(tmp_path))
    old = w.working_set
    w.working_set = WorkingSet(old.centre, old.size, old.squares)
    jobs.pop().run()
    assert not imported_ways(w, 'water')
    assert 'working area changed' in w.statusBar().currentMessage()


@pytest.mark.parametrize('category', ['water', 'coastline'])
def test_boundary_crossing_geometry_is_painted_and_pickable_only_inside(qapp, category):
    name = SquareName(10, 10)
    tags = {'waterway': 'river'} if category == 'water' else {'natural': 'coastline', 'ele': '0'}
    tags[draft.OWNER] = category
    sq = Square(name, nodes={-1: Node(-1, 10.5, 9.5, {draft.OWNER: category}),
                            -2: Node(-2, 10.5, 11.5, {draft.OWNER: category})},
                ways={-1: Way(-1, [-1, -2], tags)})
    layer = ContourLayer()
    layer.set_working_set(WorkingSet(name, 1, {name: sq}))
    image = QImage(800, 200, QImage.Format.Format_ARGB32)
    image.fill(QColor('white'))
    painter = QPainter(image)
    scale = m.scale_for_zoom(12)
    x, y = m.lonlat_to_scene(10, 10.5)
    painter.translate(200, 100)
    painter.scale(scale, scale)
    painter.translate(-x, -y)
    # Actual layer painting, including coastline arrows and its 0 m contour.
    option = QStyleOptionGraphicsItem()
    option.exposedRect = QRectF(x - 200 / scale, y - 100 / scale, 800 / scale, 200 / scale)
    layer.paint(painter, option)
    painter.end()
    assert all(image.pixelColor(px, py) == QColor('white') for px in range(0, 195) for py in range(80, 120))
    assert any(image.pixelColor(px, py) != QColor('white') for px in range(205, 700) for py in range(95, 105))
    inside, outside = m.lonlat_to_scene(10.1, 10.5), m.lonlat_to_scene(9.9, 10.5)
    pick = layer.pick_water if category == 'water' else layer.pick
    assert pick(*inside, 10 / scale) is not None
    assert pick(*outside, 10 / scale) is None
    assert layer.pick_node(*m.lonlat_to_scene(9.5, 10.5), 10 / scale) is None
    # The outside vertices remain intact for terrain processing.
    assert sq.ways[-1].refs == [-1, -2]


def test_categories_can_read_concurrently_without_dropping_an_import(window, tmp_path):
    w = window
    jobs = []
    w.draft_imports.runner = jobs.append
    path = source(tmp_path)
    w.draft_imports.import_file('water', path)
    w.draft_imports.import_file('coastline', path)
    for job in jobs:
        job.run()
    assert len(imported_ways(w, 'water')) == 1
    assert draft.sources(w.working_set, 'coastline')
