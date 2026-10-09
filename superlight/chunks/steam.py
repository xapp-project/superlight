import gettext
import os
import re
from collections import namedtuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, GObject, Gtk, Pango

from superlight import MARGIN, _, format_size
from superlight.chunks.chunk import Chunk
from superlight.common_disk import Measurer, list_drives, mount_of, read_mounts


# Tools that Steam uses to run games. They are not games, and the games need them
TOOL_NAMES = ("Proton", "Steam Linux Runtime", "Steamworks Common Redistributables")
# The other data of a game in the library, as (key, folder in steamapps, title)
EXTRAS = (("compat", "compatdata", _("Proton prefix")), ("shader", "shadercache", _("Shader cache")),
          ("mods", os.path.join("workshop", "content"), _("Workshop mods")), ("downloading", "downloading", _("Update in progress")))
LEFTOVERS = "leftovers"
# Folders that Steam creates in the libraries, which are not games
STEAM_FOLDERS = ("Steam Controller Configs", "Steamworks Shared")

Game = namedtuple("Game", ("appid", "name", "library", "install", "size", "played", "tool", "extras"))


def steam_folders():
    """Returns the folders of Steam and of its libraries, which can be on other disks. Steam as a Flatpak is not
    included, it is counted with the Flatpak apps."""
    home = GLib.get_home_dir()
    roots = []
    for candidate in (os.path.join(GLib.get_user_data_dir(), "Steam"), os.path.join(home, ".steam", "root"),
                      os.path.join(home, ".steam", "steam")):
        root = os.path.realpath(candidate)
        if os.path.isdir(root) and root not in roots:
            roots.append(root)
    folders = list(roots)
    for root in roots:
        try:
            with open(os.path.join(root, "steamapps", "libraryfolders.vdf"), encoding="utf-8", errors="replace") as source:
                libraries = re.findall(r'"path"\s+"([^"]+)"', source.read())
        except OSError:
            continue
        for library in libraries:
            library = os.path.realpath(library)
            if os.path.isdir(library) and library not in folders:
                folders.append(library)
    return folders


class GamesView(Gtk.Box):
    """The games installed with Steam, biggest first. The size of a game includes the game itself, its mods, its
    Proton prefix and its shader cache."""

    def __init__(self, window, leave):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0, border_width=MARGIN)
        self.window = window
        self.leave = leave
        self.parts = {}        # the size of each part of a game, by key: {"game": n, "compat": n, ...}
        self.owners = {}       # the game and the kind of part (game, compat..) of each measured path
        self.measurer = None

        header = Gtk.Box(spacing=MARGIN, margin_bottom=MARGIN)
        back = Gtk.Button.new_from_icon_name("xsi-go-previous-symbolic", Gtk.IconSize.BUTTON)
        back.set_tooltip_text(_("Back"))
        back.connect("clicked", lambda *_: self.go_back())
        header.pack_start(back, False, False, 0)
        title = Gtk.Label(label=_("Steam games"), xalign=0)
        title.set_attributes(Pango.AttrList.from_string("0 -1 weight bold"))
        header.pack_start(title, True, True, 0)
        self.spinner = Gtk.Spinner()
        header.pack_start(self.spinner, False, False, 0)
        refresh = Gtk.Button.new_from_icon_name("xsi-view-refresh-symbolic", Gtk.IconSize.BUTTON)
        refresh.set_tooltip_text(_("Measure again"))
        refresh.connect("clicked", lambda *_: self.load())
        header.pack_start(refresh, False, False, 0)
        self.pack_start(header, False, False, 0)

        # key, name, drive, size, last played, is a tool, is leftovers, tooltip, install folder
        self.store = Gtk.ListStore(str, str, str, GObject.TYPE_INT64, GObject.TYPE_INT64, bool, bool, str, str)
        self.sorted_store = Gtk.TreeModelSort(model=self.store)
        view = Gtk.TreeView(model=self.sorted_store, headers_visible=True)
        view.set_enable_search(False)
        view.set_tooltip_column(7)
        view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        view.get_selection().connect("changed", self.on_selection_changed)
        self.view = view
        name_renderer = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
        name_column = Gtk.TreeViewColumn(_("Name"), name_renderer, text=1)
        name_column.set_cell_data_func(name_renderer, self.style_name)
        name_column.set_sort_column_id(1)
        name_column.set_expand(True)
        name_column.set_resizable(True)
        view.append_column(name_column)
        drive_column = Gtk.TreeViewColumn(_("Drive"), Gtk.CellRendererText(), text=2)
        drive_column.set_resizable(True)
        view.append_column(drive_column)
        size_renderer = Gtk.CellRendererText(xalign=1.0)
        size_column = Gtk.TreeViewColumn(_("Size"), size_renderer)
        size_column.set_cell_data_func(size_renderer, self.format_size)
        size_column.set_sort_column_id(3)
        size_column.set_min_width(90)
        view.append_column(size_column)
        played_renderer = Gtk.CellRendererText(xalign=1.0)
        played_column = Gtk.TreeViewColumn(_("Last played"), played_renderer)
        played_column.set_cell_data_func(played_renderer, self.format_played)
        played_column.set_sort_column_id(4)
        played_column.set_min_width(110)
        view.append_column(played_column)
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
        """Reads the installed games, and measures their other data."""
        if self.measurer is not None:
            self.measurer.stop()
        # The games of all the libraries, and the folders in common/ that don't belong to a game
        games = []
        leftovers = []     # [(name, path)]
        for library in steam_folders():
            steamapps = os.path.join(library, "steamapps")
            if not os.path.isdir(steamapps):
                continue
            known_ids, known_dirs = set(), set()
            try:
                names = sorted(os.listdir(steamapps))
            except OSError:
                continue
            for name in names:
                if not (name.startswith("appmanifest_") and name.endswith(".acf")):
                    continue
                try:
                    with open(os.path.join(steamapps, name), encoding="utf-8", errors="replace") as source:
                        text = source.read()
                except OSError:
                    continue
                # Steam files use the Valve format: "key" "value", and "key" { ... } for a group
                tokens = re.findall(r'"((?:[^"\\]|\\.)*)"|([{}])', text)
                root = {}
                stack = [root]
                key = None
                for word, brace in tokens:
                    if brace == "{":
                        group = {}
                        stack[-1][key] = group
                        stack.append(group)
                        key = None
                    elif brace == "}":
                        if len(stack) > 1:
                            stack.pop()
                        key = None
                    elif key is None:
                        key = word
                    else:
                        stack[-1][key] = word
                        key = None
                state = root.get("AppState", {})
                appid = state.get("appid")
                if not appid:
                    continue
                known_ids.add(appid)
                installdir = state.get("installdir", "")
                known_dirs.add(installdir)
                extras = {}
                for kind, folder, _title in EXTRAS:
                    path = os.path.join(steamapps, folder, appid)
                    if os.path.isdir(path):
                        extras[kind] = path
                title = state.get("name") or installdir or appid
                games.append(Game(appid, title, library,
                                  os.path.join(steamapps, "common", installdir) if installdir else None,
                                  int(state.get("SizeOnDisk") or 0), int(state.get("LastPlayed") or 0),
                                  title.startswith(TOOL_NAMES), extras))
            # Folders in common/ that belong to no game
            common = os.path.join(steamapps, "common")
            if os.path.isdir(common):
                for name in sorted(os.listdir(common)):
                    path = os.path.join(common, name)
                    if os.path.isdir(path) and name not in known_dirs and name not in STEAM_FOLDERS:
                        leftovers.append((os.path.join("common", name), path))
            for _kind, folder, _title in EXTRAS:
                path = os.path.join(steamapps, folder)
                if os.path.isdir(path):
                    for name in sorted(os.listdir(path)):
                        if name.isdigit() and name not in known_ids:
                            leftovers.append((os.path.join(folder, name), os.path.join(path, name)))
        names = {drive.device: drive.name for drive in list_drives()}
        mounts = read_mounts()

        def drive_name(path):
            mount = mount_of(path, mounts)
            return names.get(mount.device, os.path.basename(path)) if mount else os.path.basename(path)

        selected = {row[0] for row in self.selected_rows()}
        self.store.clear()
        self.parts, self.owners = {}, {}
        paths = []
        for game in games:
            self.parts[game.appid] = {"game": game.size}
            self.store.append((game.appid, game.name, drive_name(game.library), game.size, game.played, game.tool, False,
                               "", game.install or ""))
            for kind, path in game.extras.items():
                self.owners[path] = (game.appid, kind)
                paths.append(path)
        if leftovers:
            self.parts[LEFTOVERS] = {}
            self.store.append((LEFTOVERS, _("Left over from removed games"), "", 0, 0, False, True, "", ""))
            for name, path in leftovers:
                self.owners[path] = (LEFTOVERS, name)
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
        owner = self.owners.get(path)
        self.pending.discard(path)
        if owner and size is not None:
            key, kind = owner
            self.parts[key][kind if key != LEFTOVERS else path] = size
            row = next((row for row in self.store if row[0] == key), None)
            if row is not None:
                self.refresh_row(row)
        if not self.pending:
            self.spinner.stop()
            self.spinner.set_visible(False)
            # Don't list the leftovers if there are none
            row = next((row for row in self.store if row[0] == LEFTOVERS), None)
            if row is not None and row[3] == 0:
                self.store.remove(row.iter)
        self.update_summary()
        return GLib.SOURCE_REMOVE

    def refresh_row(self, row):
        """Sets the size of a row to the sum of its parts. The tooltip shows each part."""
        parts = self.parts.get(row[0], {})
        row[3] = sum(parts.values())
        titles = {kind: title for kind, _folder, title in EXTRAS}
        lines = []
        if row[0] == LEFTOVERS:
            for path, size in sorted(parts.items(), key=lambda item: -item[1]):
                owner = self.owners.get(path)
                lines.append("%s: %s" % (owner[1] if owner else os.path.basename(path), format_size(size)))
            lines.append(_("The files of games that are not installed any more."))
        else:
            lines.append("%s: %s" % (_("Game"), format_size(parts.get("game", 0))))
            for kind, _folder, title in EXTRAS:
                if kind in parts and parts[kind] > 0:
                    lines.append("%s: %s" % (titles[kind], format_size(parts[kind])))
            if row[5]:
                lines.append(_("Steam uses this to run games."))
        row[7] = GLib.markup_escape_text("\n".join(lines))

    def style_name(self, _column, renderer, model, iterator, _data=None):
        tool = model.get_value(iterator, 5) or model.get_value(iterator, 6)
        renderer.set_property("sensitive", not tool)
        renderer.set_property("style", Pango.Style.ITALIC if tool else Pango.Style.NORMAL)

    def format_size(self, _column, renderer, model, iterator, _data=None):
        renderer.set_property("text", format_size(model.get_value(iterator, 3)))

    def format_played(self, _column, renderer, model, iterator, _data=None):
        played = model.get_value(iterator, 4)
        tool = model.get_value(iterator, 5) or model.get_value(iterator, 6)
        if tool:
            text = ""
        elif played:
            text = GLib.DateTime.new_from_unix_local(played).format("%x")
        else:
            text = _("Never")
        renderer.set_property("text", text)

    def selected_rows(self):
        model, paths = self.view.get_selection().get_selected_rows()
        return [model[path] for path in paths]

    def on_selection_changed(self, selection):
        self.update_summary()

    def update_summary(self):
        rows = self.selected_rows()
        if rows:
            self.summary.set_text(gettext.ngettext("%(count)d selected, %(size)s", "%(count)d selected, %(size)s", len(rows)) % {
                "count": len(rows), "size": format_size(sum(row[3] for row in rows))})
        else:
            games = [row for row in self.store if not row[5] and not row[6]]
            self.summary.set_text(gettext.ngettext("%(count)d game, %(size)s", "%(count)d games, %(size)s", len(games)) % {
                "count": len(games), "size": format_size(sum(row[3] for row in self.store))})

    def go_back(self):
        self.stop()
        self.leave()

    def stop(self):
        if self.measurer is not None:
            self.measurer.stop()


class Steam(Chunk):
    key = "steam"
    name = _("Steam")
    icon = "xsi-applications-games-symbolic"
    color = "#388ADE"

    def detect(self, taken):
        return steam_folders()

    def create_view(self, window, leave):
        return GamesView(window, leave)

    def click(self, page):
        page.show_view(self)
