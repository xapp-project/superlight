import os
import signal

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, GObject, Gtk, Pango

from superlight import MARGIN, _, format_value
from superlight.common_cgroup import assemble_tree, manager_for, unit_metadata, unit_object_path
from superlight.common_process import ProcessIcons, ProcessTable

# The columns, as (key used in the settings, title, column number in the store, kind of value, shown by default)
COLUMNS = (("name", _("Name"), 0, "text", True),
           ("description", _("Description"), 1, "text", True),
           ("processes", _("Processes"), 2, "number", True),
           ("cpu", _("CPU"), 3, "percent", True),
           ("memory", _("Memory"), 4, "size", True),
           ("oom-kills", _("OOM Kills"), 5, "number", True),
           ("oom", _("OOM Handling"), 6, "text", True),
           ("swap", _("Swap"), 7, "total", False),
           ("peak-memory", _("Peak Memory"), 8, "total", False),
           ("pressure", _("Memory Pressure"), 9, "percent", False),
           ("cgroup", _("Control Group"), 10, "text", False))
GROUP_ENDS = ("oom", "pressure")
KEYS = tuple(column[0] for column in COLUMNS)
DEFAULT_VISIBLE = tuple(column[0] for column in COLUMNS if column[4])
STORE_COLUMNS = {column[0]: column[2] for column in COLUMNS}
# The other columns of the store: the icon, and for a process its PID and start time (0 and "" for a unit)
ICON, PID, START = 11, 12, 13
# The actions, as (signal, menu entry, question, warning, button of the question, title of the error)
ACTIONS = {"end": (signal.SIGTERM, _("End"), _('End "%s"?'),
                   _("Its processes will be asked to quit. They may lose unsaved work."), _("End"),
                   _('Could not end "%s"')),
           "kill": (signal.SIGKILL, _("Kill"), _('Kill "%s"?'),
                    _("Its processes will stop immediately. Unsaved work may be lost."), _("Kill"),
                    _('Could not kill "%s"'))}
VIEWS = (("session", _("My Session")), ("all", _("Everything")))

SYSTEMD = "org.freedesktop.systemd1"
MANAGER_PATH = "/org/freedesktop/systemd1"
INTERFACES = {".scope": "Scope", ".service": "Service", ".slice": "Slice", ".socket": "Socket", ".mount": "Mount",
              ".swap": "Swap"}
BUS_TYPES = {"session": Gio.BusType.SESSION, "system": Gio.BusType.SYSTEM}


class UnitInfo:
    """What systemd says about units: their description and how systemd-oomd treats them, read over D-Bus for
    each unit and again after systemd reloads. The user's units come from the session manager, the system's from
    the system manager."""

    def __init__(self, changed):
        self.changed = changed   # called with the cgroup path when the information of a unit arrives
        self.uid = os.getuid()
        self.known = {}          # cgroup path: (description, OOM handling)
        self.pending = {}        # cgroup path: [properties of the Unit interface, of the type's interface]
        self.buses = {}

    def bus(self, manager):
        if manager not in self.buses:
            try:
                connection = Gio.bus_get_sync(BUS_TYPES[manager], None)
            except GLib.Error:
                connection = None
            else:
                self.listen(connection, manager)
            self.buses[manager] = connection
        return self.buses[manager]

    def listen(self, connection, manager):
        """systemd only tells subscribers about reloads."""
        connection.call(SYSTEMD, MANAGER_PATH, SYSTEMD + ".Manager", "Subscribe", None, None, Gio.DBusCallFlags.NONE,
                        -1, None, None)
        connection.signal_subscribe(SYSTEMD, SYSTEMD + ".Manager", "Reloading", MANAGER_PATH, None,
                                    Gio.DBusSignalFlags.NONE, self.on_reloading, manager)

    def on_reloading(self, connection, sender, object_path, interface, signal, parameters, manager):
        if not parameters.unpack()[0]:
            self.reread([path for path in self.known if manager_for(path, self.uid) == manager])

    def reread(self, paths):
        for path in paths:
            if path not in self.pending:
                self.start(path)

    def get(self, path):
        """Returns (description, OOM handling), blank until read. Starts reading the first time."""
        if path in self.known:
            return self.known[path]
        if path not in self.pending:
            self.start(path)
        return ("", "")

    def start(self, path):
        manager = manager_for(path, self.uid)
        connection = self.bus(manager) if manager else None
        if connection is None:
            self.known[path] = ("", "")
            return
        name = path.rsplit("/", 1)[-1]
        self.pending[path] = [None, None]
        for index, interface in enumerate(("Unit", INTERFACES[name[name.rfind("."):]])):
            connection.call(SYSTEMD, unit_object_path(name), "org.freedesktop.DBus.Properties", "GetAll",
                            GLib.Variant("(s)", [SYSTEMD + "." + interface]), GLib.VariantType("(a{sv})"),
                            Gio.DBusCallFlags.NONE, 5000, None, self.on_properties, (path, index))

    def on_properties(self, connection, result, data):
        path, index = data
        try:
            properties = connection.call_finish(result).unpack()[0]
        except GLib.Error:
            properties = {}
        if path not in self.pending:
            return
        self.pending[path][index] = properties
        if None not in self.pending[path]:
            self.known[path] = unit_metadata(*self.pending.pop(path))
            self.changed(path)

    def forget(self, path):
        self.known.pop(path, None)
        self.pending.pop(path, None)


class SystemdPage(ProcessTable, Gtk.Box):
    PREFIX = "systemd"
    COLUMNS = COLUMNS
    KEYS = KEYS
    GROUP_ENDS = GROUP_ENDS
    DEFAULT_VISIBLE = DEFAULT_VISIBLE
    STORE_COLUMNS = STORE_COLUMNS
    ACTIONS = ACTIONS
    CONTEXT_ACTIONS = ("end", "kill")

    def __init__(self, window, settings, scanner):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0, border_width=MARGIN)
        self.window = window
        self.settings = settings
        self.scanner = scanner
        self.uid = os.getuid()
        self.units = UnitInfo(self.on_unit_info)
        self.icons = ProcessIcons()
        self.unit_icon = Gio.ThemedIcon.new("emblem-system-symbolic")
        self.process_store = Gtk.TreeStore(str, str, int, float, GObject.TYPE_INT64, int, str, GObject.TYPE_INT64,
                                           GObject.TYPE_INT64, float, str, Gio.Icon, int, str)
        self.iters = {}      # the row of each unit (by cgroup path) and process (by (pid, start))
        self.parents = {}    # the parent key of each row, or None
        self.signaled = set()   # the rows asked to end or killed that are still there
        self.show_tree = True
        self.expand_when_filled = True
        self.search_text = ""
        self.session_only = self.settings.get_string("systemd-view") != "all"
        self.filter = self.process_store.filter_new()
        self.filter.set_visible_func(self.matches_search)
        self.sorted_store = Gtk.TreeModelSort(model=self.filter)
        view = Gtk.TreeView(model=self.sorted_store, headers_visible=True)
        view.set_enable_search(False)
        view.connect("button-press-event", self.on_view_pressed)
        self.columns = {}
        for key, title, number, kind, _default in COLUMNS:
            renderer = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END,
                                            width_chars=20 if key in ("name", "description") else 12)
            column = Gtk.TreeViewColumn(title)
            if key == "name":
                icon_renderer = Gtk.CellRendererPixbuf()
                icon_renderer.set_property("stock-size", Gtk.IconSize.MENU)
                column.pack_start(icon_renderer, False)
                column.add_attribute(icon_renderer, "gicon", ICON)
                column.set_cell_data_func(icon_renderer, self.style_icon)
            column.pack_start(renderer, True)
            column.set_cell_data_func(renderer, self.format_cell, (number, kind))
            if kind != "text":
                renderer.set_property("xalign", 1.0)
            column.set_sort_column_id(number)
            column.set_resizable(True)
            column.set_reorderable(True)
            column.set_expand(key == "name")
            column.key = key
            column.get_button().connect("button-press-event", self.on_header_pressed)
            self.columns[key] = column
            view.append_column(column)
        self.view = view
        view.set_expander_column(self.columns["name"])
        self.restore_columns()
        view.connect("columns-changed", self.on_columns_changed)
        sort_column = self.settings.get_string("systemd-sort-column")
        self.sorted_store.set_sort_column_id(
            STORE_COLUMNS.get(sort_column, STORE_COLUMNS["memory"]),
            Gtk.SortType.ASCENDING if self.settings.get_boolean("systemd-sort-order-ascending") else Gtk.SortType.DESCENDING)
        self.sorted_store.connect("sort-column-changed", self.on_sort_changed)
        self.selection = view.get_selection()
        self.selection.set_mode(Gtk.SelectionMode.SINGLE)
        self.selection.connect("changed", self.on_selected)
        scroll = Gtk.ScrolledWindow()
        scroll.set_overlay_scrolling(False)
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_shadow_type(Gtk.ShadowType.IN)
        scroll.add(view)
        self.pack_start(scroll, True, True, 0)

        bar = Gtk.Box(spacing=MARGIN, margin_top=MARGIN)
        self.search_entry = Gtk.SearchEntry(placeholder_text=_("Search"), width_chars=24)
        view.connect("key-press-event", lambda _view, event: self.search_entry.handle_event(event))
        self.search_entry.connect("search-changed", self.on_search_changed)
        self.search_entry.connect("stop-search", self.on_search_stopped)
        bar.pack_start(self.search_entry, True, True, 0)
        self.view_combo = Gtk.ComboBoxText()
        for key, title in VIEWS:
            self.view_combo.append(key, title)
        self.view_combo.set_active_id("session" if self.session_only else "all")
        self.view_combo.connect("changed", self.on_view_changed)
        bar.pack_start(self.view_combo, False, False, 0)
        self.end_button = Gtk.Button(label=_("End"), sensitive=False)
        self.end_button.connect("clicked", lambda *_: self.signal_selected("end"))
        self.end_button.get_style_context().add_class("destructive-action")
        menu, self.action_items = self.actions_menu(("kill",))
        menu.connect("show", lambda *_: self.update_actions(self.action_items))
        self.more_button = Gtk.MenuButton(popup=menu, sensitive=False)
        self.more_button.set_image(Gtk.Image.new_from_icon_name("xsi-pan-down-symbolic", Gtk.IconSize.BUTTON))
        self.more_button.get_style_context().add_class("destructive-action")
        split = Gtk.Box()
        split.get_style_context().add_class("linked")
        split.pack_start(self.end_button, False, False, 0)
        split.pack_start(self.more_button, False, False, 0)
        bar.pack_end(split, False, False, 0)
        self.pack_start(bar, False, False, 0)

    @staticmethod
    def row_key(row):
        """The key of a row: the cgroup path of a unit, (pid, start) of a process."""
        return row[STORE_COLUMNS["cgroup"]] if row[PID] == 0 else (row[PID], row[START])

    def row_matches(self, row):
        return not self.search_text or any(
            self.search_text in row[number].casefold()
            for number in (STORE_COLUMNS["name"], STORE_COLUMNS["description"], STORE_COLUMNS["cgroup"]))

    def style_icon(self, _column, renderer, model, iterator, _data=None):
        renderer.set_property("sensitive", self.row_key(model[iterator]) not in self.signaled)

    def format_cell(self, _column, renderer, model, iterator, data):
        number, kind = data
        value = model.get_value(iterator, number)
        signaled = self.row_key(model[iterator]) in self.signaled
        renderer.set_property("sensitive", not signaled)
        renderer.set_property("strikethrough", signaled)
        # A value that is not known (-1) is left blank
        renderer.set_property("text", "" if kind in ("number", "percent", "size") and value < 0
                              else format_value(kind, value))

    def on_view_changed(self, combo):
        self.session_only = combo.get_active_id() != "all"
        self.settings.set_string("systemd-view", combo.get_active_id() or "session")
        self.expand_when_filled = True
        self.refresh()

    def can_signal(self, row):
        """Only processes and the units of the user's own systemd can be ended."""
        return row[PID] > 0 or manager_for(row[STORE_COLUMNS["cgroup"]], self.uid) == "session"

    def on_selected(self, selection):
        model, iterator = selection.get_selected()
        allowed = iterator is not None and self.can_signal(model[iterator])
        self.end_button.set_sensitive(allowed)
        self.more_button.set_sensitive(allowed)

    def update_actions(self, items):
        model, iterator = self.selection.get_selected()
        allowed = iterator is not None and self.can_signal(model[iterator])
        for item in items.values():
            item.set_sensitive(allowed)

    def signal_selected(self, action):
        model, iterator = self.selection.get_selected()
        if iterator is None or not self.can_signal(model[iterator]):
            return
        row = model[iterator]
        key, name = self.row_key(row), row[STORE_COLUMNS["name"]]
        self.confirm(action, name, lambda: self.perform(action, key, name))

    def perform(self, action, key, name):
        if isinstance(key, tuple):
            try:
                self.signal_process(*key, ACTIONS[action][0])
                self.signaled.add(key)
            except (OSError, ValueError) as error:
                self.show_error(ACTIONS[action][5] % name, str(error))
            self.refresh()
            return
        connection = self.units.bus("session")
        if connection is None:
            self.show_error(ACTIONS[action][5] % name, _("The session's systemd could not be reached."))
            return
        if action == "end":
            method, arguments = "StopUnit", GLib.Variant("(ss)", [name, "replace"])
        else:
            method, arguments = "KillUnit", GLib.Variant("(ssi)", [name, "all", signal.SIGKILL])
        connection.call(SYSTEMD, MANAGER_PATH, SYSTEMD + ".Manager", method, arguments, None,
                        Gio.DBusCallFlags.NONE, -1, None, self.on_unit_controlled, (action, key, name))
        self.signaled.add(key)
        self.view.queue_draw()

    def on_unit_controlled(self, connection, result, data):
        action, key, name = data
        try:
            connection.call_finish(result)
        except GLib.Error as error:
            self.signaled.discard(key)
            self.show_error(ACTIONS[action][5] % name, error.message)
        self.refresh()

    def on_unit_info(self, path):
        iterator = self.iters.get(path)
        if iterator is not None:
            self.process_store.set(iterator, [STORE_COLUMNS["description"], STORE_COLUMNS["oom"]],
                                   list(self.units.known[path]))

    def features(self):
        wanted = {"cgroups", "command"}
        if self.columns["swap"].get_visible():
            wanted.add("swap")
        return wanted

    def refresh(self):
        self.scanner.request(self.features(), self.on_scanned)

    def on_scanned(self, processes, cgroups):
        cgroups, processes, parents, counts = assemble_tree(cgroups, processes, self.uid, self.session_only)
        rows = {}
        for c in cgroups:
            description, oom = self.units.get(c.path)
            rows[c.path] = (c.name, description, counts[c.path], c.cpu, c.memory, c.oom_kills, oom, c.swap, c.peak,
                            c.pressure, c.path, self.unit_icon, 0, "")
        for p in processes:
            rows[p.key] = (p.name, p.command, -1, p.cpu, p.memory, -1, "", p.swap, -1, -1.0, p.cgroup,
                           self.icons.icon_for(p), p.pid, p.start)
        self.signaled &= set(rows)
        for path in [path for path in self.units.known if path not in rows]:
            self.units.forget(path)
        self.sync_rows(rows, parents)
        if self.expand_when_filled and rows:
            self.expand_when_filled = False
            self.view.expand_all()
