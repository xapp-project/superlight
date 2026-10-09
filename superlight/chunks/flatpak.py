import gettext
import os
from collections import namedtuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, GObject, Gtk, Pango

from superlight import MARGIN, _, format_size
from superlight.chunks.chunk import Chunk
from superlight.common_disk import Measurer

try:
    gi.require_version("Flatpak", "1.0")
    from gi.repository import Flatpak as FlatpakLib
except (ValueError, ImportError):
    FlatpakLib = None   # Without libflatpak, the Flatpak view is not available

Ref = namedtuple("Ref", ("key", "name", "id", "branch", "app", "user", "size"))

def available():
    return FlatpakLib is not None

class FlatpakView(Gtk.Box):
    """The list of Flatpak apps and runtimes, biggest first. The size of an app is the install plus the data it
    keeps in ~/.var/app."""

    def __init__(self, window, leave):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0, border_width=MARGIN)
        self.window = window
        self.leave = leave
        self.data = {}          # the size of the data of each app in ~/.var/app, by key
        self.sizes = {}         # the size of each install, by key
        self.owners = {}        # the key of the app that owns each measured path
        self.measurer = None
        self.pending = set()

        header = Gtk.Box(spacing=MARGIN, margin_bottom=MARGIN)
        back = Gtk.Button.new_from_icon_name("xsi-go-previous-symbolic", Gtk.IconSize.BUTTON)
        back.set_tooltip_text(_("Back to the overview"))
        back.connect("clicked", lambda *_: self.go_back())
        header.pack_start(back, False, False, 0)
        title = Gtk.Label(label=_("Flatpak"), xalign=0)
        title.set_attributes(Pango.AttrList.from_string("0 -1 weight bold"))
        header.pack_start(title, True, True, 0)
        self.spinner = Gtk.Spinner()
        header.pack_start(self.spinner, False, False, 0)
        refresh = Gtk.Button.new_from_icon_name("xsi-view-refresh-symbolic", Gtk.IconSize.BUTTON)
        refresh.set_tooltip_text(_("Measure again"))
        refresh.connect("clicked", lambda *_: self.load())
        header.pack_start(refresh, False, False, 0)
        self.pack_start(header, False, False, 0)

        # key, name, type, installation, size, tooltip, is a runtime
        self.store = Gtk.ListStore(str, str, str, str, GObject.TYPE_INT64, str, bool)
        self.sorted_store = Gtk.TreeModelSort(model=self.store)
        view = Gtk.TreeView(model=self.sorted_store, headers_visible=True)
        view.set_enable_search(False)
        view.set_tooltip_column(5)
        view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        view.get_selection().connect("changed", lambda *_: self.update_summary())
        self.view = view
        name_renderer = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
        name_column = Gtk.TreeViewColumn(_("Name"), name_renderer, text=1)
        name_column.set_cell_data_func(name_renderer, self.style_name)
        name_column.set_sort_column_id(1)
        name_column.set_expand(True)
        name_column.set_resizable(True)
        view.append_column(name_column)
        for title, number in ((_("Type"), 2), (_("Installation"), 3)):
            column = Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=number)
            column.set_resizable(True)
            view.append_column(column)
        size_renderer = Gtk.CellRendererText(xalign=1.0)
        size_column = Gtk.TreeViewColumn(_("Size"), size_renderer)
        size_column.set_cell_data_func(size_renderer, self.format_size)
        size_column.set_sort_column_id(4)
        size_column.set_min_width(90)
        view.append_column(size_column)
        # Biggest first
        self.sorted_store.set_sort_column_id(size_column.get_sort_column_id(), Gtk.SortType.DESCENDING)
        scroll = Gtk.ScrolledWindow()
        scroll.set_overlay_scrolling(False)
        scroll.set_shadow_type(Gtk.ShadowType.IN)
        scroll.add(view)
        self.pack_start(scroll, True, True, 0)

        bar = Gtk.Box(spacing=MARGIN, margin_top=MARGIN)
        self.summary = Gtk.Label(xalign=0)
        self.summary.get_style_context().add_class("dim-label")
        bar.pack_start(self.summary, True, True, 0)
        self.pack_start(bar, False, False, 0)

    def load(self):
        """Reads what is installed, and measures the data of the apps."""
        if self.measurer is not None:
            self.measurer.stop()
        # The user installation and the system installations
        installations = [FlatpakLib.Installation.new_user()]
        try:
            installations += FlatpakLib.get_system_installations(None)
        except GLib.Error:
            pass
        refs = []
        extensions = {}   # the size of the .Locale extension of each app, by installation, name and branch. Added to its size.
        for installation in installations:
            try:
                installed = installation.list_installed_refs(None)
            except GLib.Error:
                continue
            for ref in installed:
                if ref.get_name().endswith(".Locale"):
                    key = (installation.get_id(), ref.get_name()[:-len(".Locale")], ref.get_branch())
                    extensions[key] = extensions.get(key, 0) + ref.get_installed_size()
                    continue
                app = ref.get_kind() == FlatpakLib.RefKind.APP
                name = ref.get_appdata_name() or ref.get_name()
                # Runtimes have several versions. Add the branch to the name if it isn't in it already.
                if not app and ref.get_branch() not in name:
                    name = "%s %s" % (name, ref.get_branch())
                refs.append(Ref("%s:%s" % (installation.get_id(), ref.format_ref()), name, ref.get_name(),
                                ref.get_branch(), app, installation.get_is_user(), ref.get_installed_size()))
        refs = [ref._replace(size=ref.size + extensions.get((ref.key.split(":", 1)[0], ref.id, ref.branch), 0))
                for ref in refs]
        selected = {row[0] for row in self.selected_rows()}
        self.store.clear()
        self.data, self.owners = {}, {}
        paths = []
        self.sizes = {ref.key: ref.size for ref in refs}
        for ref in refs:
            self.store.append((ref.key, ref.name, _("App") if ref.app else _("Runtime"),
                               _("User") if ref.user else _("System"), ref.size, "", not ref.app))
            if ref.app:
                path = os.path.join(GLib.get_home_dir(), ".var", "app", ref.id)
                if os.path.isdir(path):
                    self.owners[path] = ref.key
                    paths.append(path)
        for row in self.store:
            self.refresh_row(row)
        for row in self.sorted_store:
            if row[0] in selected:
                self.view.get_selection().select_iter(row.iter)
        self.measurer = Measurer(self.on_measured)
        self.pending = set(paths)
        self.spinner.set_visible(bool(paths))
        self.spinner.start() if paths else self.spinner.stop()
        self.measurer.measure(sorted(paths))
        self.update_summary()

    def on_measured(self, path, size):
        key = self.owners.get(path)
        self.pending.discard(path)
        if key and size is not None:
            self.data[key] = size
            row = next((row for row in self.store if row[0] == key), None)
            if row is not None:
                self.refresh_row(row)
        if not self.pending:
            self.spinner.stop()
            self.spinner.set_visible(False)
        self.update_summary()
        return GLib.SOURCE_REMOVE

    def refresh_row(self, row):
        """Sets the size of a row: the install plus the data. The tooltip shows both."""
        installed, kept = self.sizes.get(row[0], 0), self.data.get(row[0], 0)
        row[4] = installed + kept
        lines = ["%s: %s" % (_("Install"), format_size(installed))]
        if kept > 0:
            lines.append("%s: %s" % (_("Data"), format_size(kept)))
        if row[6]:
            lines.append(_("What apps run on, and extensions of it."))
        row[5] = GLib.markup_escape_text("\n".join(lines))

    def style_name(self, _column, renderer, model, iterator, _data=None):
        runtime = model.get_value(iterator, 6)
        renderer.set_property("sensitive", not runtime)
        renderer.set_property("style", Pango.Style.ITALIC if runtime else Pango.Style.NORMAL)

    def format_size(self, _column, renderer, model, iterator, _data=None):
        renderer.set_property("text", format_size(model.get_value(iterator, 4)))

    def selected_rows(self):
        model, paths = self.view.get_selection().get_selected_rows()
        return [model[path] for path in paths]

    def update_summary(self):
        rows = self.selected_rows()
        if rows:
            self.summary.set_text(gettext.ngettext("%(count)d selected, %(size)s", "%(count)d selected, %(size)s", len(rows)) % {
                "count": len(rows), "size": format_size(sum(row[4] for row in rows))})
        else:
            apps = [row for row in self.store if not row[6]]
            self.summary.set_text(gettext.ngettext("%(count)d app, %(size)s", "%(count)d apps, %(size)s", len(apps)) % {
                "count": len(apps), "size": format_size(sum(row[4] for row in self.store))})

    def go_back(self):
        self.stop()
        self.leave()

    def stop(self):
        if self.measurer is not None:
            self.measurer.stop()


class Flatpak(Chunk):
    key = "flatpak"
    name = _("Flatpak")
    icon = "xsi-package-x-generic-symbolic"
    color = "#1C9E75"

    def detect(self, taken):
        return [os.path.join(GLib.get_home_dir(), ".var", "app"), os.path.join(GLib.get_user_data_dir(), "flatpak"),
                "/var/lib/flatpak"]

    def create_view(self, window, leave):
        return FlatpakView(window, leave)

    def click(self, page):
        if available():
            page.show_view(self)
