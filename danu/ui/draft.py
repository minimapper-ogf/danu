"""Local JOSM draft imports: read on a worker, replace on the undo stack."""

from pathlib import Path

from PySide6.QtCore import QObject, QThreadPool
from PySide6.QtWidgets import QFileDialog, QMessageBox

from ..water import draft
from .background import Job


class DraftImports(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.runner = QThreadPool.globalInstance().start
        self.jobs = {}
        self.serials = dict.fromkeys(draft.CATEGORIES, 0)

    def add_menu(self, parent):
        menu = parent.addMenu('JOSM &drafts')
        for category, title in draft.CATEGORIES.items():
            sub = menu.addMenu(title.capitalize())
            source = sub.addAction('No draft imported')
            source.setEnabled(False)
            sub.addSeparator()
            load = sub.addAction(f'Import {title.capitalize()} from .osm File…')
            load.triggered.connect(lambda _=False, c=category: self.import_file(c))
            reload = sub.addAction('Reload file')
            reload.triggered.connect(lambda _=False, c=category: self.reload(c))
            remove = sub.addAction('Remove imported features')
            remove.triggered.connect(lambda _=False, c=category: self.remove(c))

            def update(c=category, label=source, load=load, reload=reload, remove=remove):
                ws = self.window.working_set
                paths = self.paths(c)
                names = draft.source_names(ws, c) if ws else []
                label.setText(', '.join(names) if names else 'No draft imported')
                label.setToolTip('\n'.join(paths))
                load.setEnabled(ws is not None)
                reload.setEnabled(len(paths) == 1)
                remove.setEnabled(bool(names))
            sub.aboutToShow.connect(update)
        return menu

    def paths(self, category):
        ws = self.window.working_set
        keys = draft.sources(ws, category) if ws else []
        paths = [self.window.settings.q.value(f'draft_sources/{key}', '') for key in keys]
        # Another machine can open the geometry without having the source file.
        return [str(p) for p in paths] if all(paths) else []

    def import_file(self, category, path=None):
        if self.window.working_set is None:
            self.window.statusBar().showMessage('open a square first')
            return False
        if path is None:
            paths = self.paths(category)
            path, _ = QFileDialog.getOpenFileName(
                self.window, f'Import draft {draft.CATEGORIES[category]}',
                paths[0] if len(paths) == 1 else '', 'OSM files (*.osm *.osm.xz)')
        if not path:
            return False
        path = Path(path).resolve()
        ws = self.window.working_set
        self.serials[category] += 1
        serial = self.serials[category]
        names = tuple(ws.squares)
        self.window.statusBar().showMessage(f'reading draft {draft.CATEGORIES[category]} from {path.name}…')

        def read():
            result = draft.read(path, category)
            return result, draft.place(result, names)

        def done(answer):
            if serial != self.serials[category]:
                return
            if ws is not self.window.working_set:
                self.window.statusBar().showMessage('the working area changed; import the draft again')
                return
            result, placed = answer
            if result.broken and not self.accept_valid(result.broken):
                self.window.statusBar().showMessage('draft import cancelled; previous features kept')
                return
            # A modal report can run other events, including opening another set.
            if serial != self.serials[category] or ws is not self.window.working_set:
                return
            alloc = self.window.editor.history.alloc(ws.squares[ws.centre])
            steps = draft.commands(ws, category, placed, path, alloc)
            self.window.editor.do_across(steps)
            self.window.settings.q.setValue(f'draft_sources/{draft.source_key(path)}', str(path))
            count = len(result.roots) - result.outside
            self.window.statusBar().showMessage(
                f'imported {count} draft {draft.CATEGORIES[category]} from {path.name}; '
                f'{result.outside} outside the working area, {len(result.broken)} broken, skipped. '
                'Previous draft replaced; Ctrl+Z takes it back')

        def failed(reason):
            if serial == self.serials[category]:
                self.window.statusBar().showMessage('draft import failed; previous features kept')
                QMessageBox.warning(self.window, 'Draft import failed', reason)

        # Release finished jobs on the next request, outside their own signal.
        # Running jobs may still emit, but their stale serial cannot change data.
        if category in self.jobs:
            self.jobs[category].release()
        self.jobs[category] = Job(read, done, failed)
        self.runner(self.jobs[category])
        return True

    def accept_valid(self, broken):
        box = QMessageBox(self.window)
        box.setWindowTitle('Incomplete draft geometry')
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f'{len(broken)} broken feature(s) cannot be imported.')
        box.setInformativeText('Import the valid features and skip the broken ones, or keep the previous draft?')
        box.setDetailedText('\n'.join(broken))
        valid = box.addButton('Import valid features only', QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        return box.clickedButton() is valid

    def reload(self, category):
        paths = self.paths(category)
        if len(paths) != 1:
            self.window.statusBar().showMessage('choose a draft file to replace the sources in this working area')
            return False
        return self.import_file(category, paths[0])

    def remove(self, category):
        self.serials[category] += 1
        ws = self.window.working_set
        if ws is None:
            return False
        alloc = self.window.editor.history.alloc(ws.squares[ws.centre])
        steps = draft.commands(ws, category, {}, None, alloc)
        self.window.editor.do_across(steps)
        self.window.statusBar().showMessage(f'removed draft {draft.CATEGORIES[category]}; Ctrl+Z takes it back')
        return bool(steps)

    def detach(self):
        for category in self.serials:
            self.serials[category] += 1
        for job in self.jobs.values():
            job.release()
        self.jobs.clear()
