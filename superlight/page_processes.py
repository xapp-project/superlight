import os
import shlex
import signal

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GObject, Gtk, Pango

from superlight import MARGIN, _, format_value
from superlight.common_proc import process_stat, username
from superlight.common_process import ProcessIcons, ProcessTable


# The columns, as (key used in the settings, title, column number in the store, kind of value, shown by default)
COLUMNS = (("name", _("Name"), 0, "text", True),
           ("pid", _("PID"), 1, "number", True),
           ("user", _("User"), 2, "text", True),
           ("state", _("State"), 7, "state", False),
           ("priority", _("Priority"), 8, "priority", False),
           # Translators: Process
           ("started", _("Started"), 10, "time", False),
           ("unit", _("Systemd Unit"), 15, "text", False),
           ("cgroup", _("Control Group"), 26, "text", False),
           ("command", _("Command Line"), 14, "text", False),
           ("cpu", _("CPU"), 3, "percent", True),
           ("cpu-time", _("CPU Time"), 9, "duration", False),
           ("memory", _("Memory"), 4, "size", True),
           ("virtual", _("Virtual Memory"), 11, "size", False),
           ("resident", _("Resident Memory"), 12, "size", False),
           ("shared", _("Shared Memory"), 13, "size", False),
           ("swap", _("Swap"), 20, "total", False),
           ("disk-read", _("Disk Read"), 16, "rate", False),
           ("disk-write", _("Disk Write"), 17, "rate", False),
           ("disk-read-total", _("Disk Read Total"), 18, "total", False),
           ("disk-write-total", _("Disk Write Total"), 19, "total", False),
           ("gpu", _("GPU"), 21, "gpu", False),
           ("video-memory", _("Video Memory"), 22, "total", False),
           ("video-encoder", _("Video Encoder"), 23, "gpu", False),
           ("video-decoder", _("Video Decoder"), 24, "gpu", False))
# In the menu of the columns, a separator is added after each of these
GROUP_ENDS = ("command", "cpu-time", "swap", "disk-write-total")
UNIT_KEYS = ("unit", "cgroup")
IO_KEYS = ("disk-read", "disk-write", "disk-read-total", "disk-write-total")
GPU_KEYS = ("gpu", "video-memory", "video-encoder", "video-decoder")

KEYS = tuple(column[0] for column in COLUMNS)
DEFAULT_VISIBLE = tuple(column[0] for column in COLUMNS if column[4])
STORE_COLUMNS = {column[0]: column[2] for column in COLUMNS}
# The actions, as (signal, menu entry, question, warning, button of the question, title of the error). The question
# is None for actions that don't ask first.
ACTIONS = {"end": (signal.SIGTERM, _("End Process"), _('End "%s"?'),
                   _("The process will be asked to quit. It may lose unsaved work."), _("End Process"),
                   _("Could not end the process")),
           "kill": (signal.SIGKILL, _("Kill Process"), _('Kill "%s"?'),
                    _("The process will stop immediately. Unsaved work may be lost."), _("Kill Process"),
                    _("Could not kill the process")),
           "stop": (signal.SIGSTOP, _("Stop Process"), None, None, None, _("Could not stop the process")),
           "continue": (signal.SIGCONT, _("Continue Process"), None, None, None,
                        _("Could not continue the process"))}
# Which processes can be shown, as (key used in the settings, title)
VIEWS = (("all", _("All Processes")), ("mine", _("My Processes")), ("active", _("Active Processes")))


def process_row(p, icon):
    """The values of the store columns for a process."""
    return (p.name, p.pid, p.user, p.cpu, p.memory, p.start, icon, p.state, p.nice, p.cpu_time, p.started,
            p.virtual, p.resident, p.shared, p.command, p.unit, p.disk_read, p.disk_write, p.disk_read_total,
            p.disk_write_total, p.swap, p.gpu, p.video_memory, p.video_encoder, p.video_decoder, p.parent, p.cgroup)

class ProcessesPage(ProcessTable, Gtk.Box):
    PREFIX = "processes"
    COLUMNS = COLUMNS
    KEYS = KEYS
    GROUP_ENDS = GROUP_ENDS
    DEFAULT_VISIBLE = DEFAULT_VISIBLE
    STORE_COLUMNS = STORE_COLUMNS
    ACTIONS = ACTIONS
    STOP_TOOLTIP = _("Pauses the process")

    def __init__(self, window, settings, scanner):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0, border_width=MARGIN)
        self.window = window
        self.settings = settings
        self.scanner = scanner
        # name, PID, user, CPU, memory, start, icon, then the values of the other columns, the parent PID and the
        # control group
        self.process_store = Gtk.TreeStore(str, int, str, float, GObject.TYPE_UINT64, str, Gio.Icon,
                                           str, int, float, GObject.TYPE_UINT64, GObject.TYPE_UINT64,
                                           GObject.TYPE_UINT64, GObject.TYPE_UINT64, str, str, float, float,
                                           GObject.TYPE_INT64, GObject.TYPE_INT64, GObject.TYPE_INT64, float,
                                           GObject.TYPE_INT64, float, float, int, str)
        self.icons = ProcessIcons()
        self.signaled = {}   # processes asked to end or killed that still run, by (pid, start)

        self.search_text = ""
        self.show_tree = self.settings.get_boolean("processes-tree")
        self.iters = {}      # the row of each process in the store, by (pid, start)
        self.parents = {}    # the parent of each process in the store, as (pid, start), or None
        self.expand_when_filled = self.show_tree
        self.view_mode = self.settings.get_string("processes-view")
        self.user = username(os.getuid())
        self.filter = self.process_store.filter_new()
        self.filter.set_visible_func(self.matches_search)
        self.sorted_store = Gtk.TreeModelSort(model=self.filter)
        view = Gtk.TreeView(model=self.sorted_store, headers_visible=True)
        view.set_enable_search(False)   # typing goes to the search entry
        view.set_show_expanders(self.show_tree)
        view.set_has_tooltip(True)
        view.connect("query-tooltip", self.process_tooltip)
        view.connect("button-press-event", self.on_view_pressed)
        self.columns = {}
        for key, title, number, kind, _default in COLUMNS:
            # A name can be a whole command line. Cut it.
            renderer = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END, width_chars=12)
            column = Gtk.TreeViewColumn(title)
            if key == "name":
                icon_renderer = Gtk.CellRendererPixbuf()
                icon_renderer.set_property("stock-size", Gtk.IconSize.MENU)
                column.pack_start(icon_renderer, False)
                column.add_attribute(icon_renderer, "gicon", 6)
                column.set_cell_data_func(icon_renderer, self.style_icon)
            column.pack_start(renderer, True)
            column.set_cell_data_func(renderer, self.format_process_cell, (number, kind))
            if kind in ("number", "percent", "size", "duration", "rate", "total", "gpu"):
                renderer.set_property("xalign", 1.0)
            column.set_sort_column_id(number)
            column.set_resizable(True)
            column.set_reorderable(True)
            column.set_expand(key in ("name", "command", "unit", "cgroup"))
            column.key = key
            column.get_button().connect("button-press-event", self.on_header_pressed)
            self.columns[key] = column
            view.append_column(column)
        self.view = view
        view.set_expander_column(self.columns["name"])
        self.restore_columns()
        view.connect("columns-changed", self.on_columns_changed)
        sort_column = self.settings.get_string("processes-sort-column")
        self.sorted_store.set_sort_column_id(
            STORE_COLUMNS.get(sort_column, STORE_COLUMNS["memory"]),
            Gtk.SortType.ASCENDING if self.settings.get_boolean("processes-sort-order-ascending") else Gtk.SortType.DESCENDING)
        self.sorted_store.connect("sort-column-changed", self.on_sort_changed)
        self.process_selection = view.get_selection()
        self.process_selection.set_mode(Gtk.SelectionMode.SINGLE)
        self.process_selection.connect("changed", self.process_selected)
        scroll = Gtk.ScrolledWindow()
        scroll.set_overlay_scrolling(False)
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(view)
        scroll.set_shadow_type(Gtk.ShadowType.IN)
        self.pack_start(scroll, True, True, 0)
        bar = Gtk.Box(spacing=MARGIN, margin_top=MARGIN)
        self.search_entry = Gtk.SearchEntry(placeholder_text=_("Search"), width_chars=24)
        # Typing in the list goes to the search entry
        view.connect("key-press-event", lambda _view, event: self.search_entry.handle_event(event))
        self.search_entry.connect("search-changed", self.on_search_changed)
        self.search_entry.connect("stop-search", self.on_search_stopped)
        bar.pack_start(self.search_entry, True, True, 0)
        self.view_combo = Gtk.ComboBoxText()
        for key, title in VIEWS:
            self.view_combo.append(key, title)
        self.view_combo.set_active_id(self.view_mode if self.view_mode in dict(VIEWS) else "all")
        self.view_combo.connect("changed", self.on_view_changed)
        bar.pack_start(self.view_combo, False, False, 0)
        self.tree_check = Gtk.CheckButton(label=_("Process tree"), active=self.show_tree,
                                          tooltip_text=_("Show each process under the one that started it"))
        self.tree_check.connect("toggled", self.on_tree_toggled)
        bar.pack_start(self.tree_check, False, False, 0)
        # The main button ends the process. The other actions are in the menu next to it.
        self.end_button = Gtk.Button(label=_("End Process"))
        self.end_button.set_sensitive(False)
        self.end_button.connect("clicked", lambda *_: self.signal_selected("end"))
        self.end_button.get_style_context().add_class("destructive-action")
        menu, self.action_items = self.actions_menu(("kill", "stop", "continue"))
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
        self.refresh()

    def row_matches(self, row):
        """Returns True if the row is allowed by the selected view (all, mine, active) and by the search text, which
        can be in the name, the user, the PID or the command line."""
        if self.view_mode == "mine" and row[2] != self.user:
            return False
        # Active means using the CPU, or running, right now
        if self.view_mode == "active" and not (row[3] > 0 or row[7] == "R"):
            return False
        if not self.search_text:
            return True
        return any(self.search_text in str(field).casefold() for field in (row[0], row[2], row[1], row[14]))

    def on_tree_toggled(self, button):
        self.show_tree = button.get_active()
        self.settings.set_boolean("processes-tree", self.show_tree)
        self.view.set_show_expanders(self.show_tree)
        # Add all the rows again, with or without parents
        self.process_store.clear()
        self.iters = {}
        self.parents = {}
        self.refresh()
        self.expand_when_filled = self.show_tree

    def on_view_changed(self, combo):
        self.view_mode = combo.get_active_id() or "all"
        self.settings.set_string("processes-view", self.view_mode)
        self.filter.refilter()
        if self.show_tree:
            self.view.expand_all()

    def style_icon(self, _column, renderer, model, iterator, _data=None):
        renderer.set_property("sensitive", self.is_active(model, iterator))

    def is_active(self, model, iterator):
        """Returns True if the process is running normally: not paused, and not asked to end."""
        paused = model.get_value(iterator, 7) in ("T", "t")
        return not paused and (model.get_value(iterator, 1), model.get_value(iterator, 5)) not in self.signaled

    def format_process_cell(self, _column, renderer, model, iterator, data):
        number, kind = data
        value = model.get_value(iterator, number)
        # Paused: gray and italic. Asked to end and still running: crossed out
        renderer.set_property("sensitive", self.is_active(model, iterator))
        renderer.set_property("style", Pango.Style.ITALIC if model.get_value(iterator, 7) in ("T", "t")
                              else Pango.Style.NORMAL)
        renderer.set_property("strikethrough",
                              (model.get_value(iterator, 1), model.get_value(iterator, 5)) in self.signaled)
        renderer.set_property("text", format_value(kind, value))

    def process_tooltip(self, view, x, y, keyboard_mode, tooltip):
        found, _x, _y, model, path, iterator = view.get_tooltip_context(x, y, keyboard_mode)
        if not found:
            return False
        pid, start = model[iterator][1], model[iterator][5]
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as source:
                arguments = source.read().rstrip(b"\0").split(b"\0")
            if process_stat(pid)[2] != start:
                return False
            command = shlex.join(arg.decode("utf-8", errors="replace") for arg in arguments)
            if arguments == [b""]:
                command = f"[{model[iterator][0]}]"
        except (OSError, ValueError, IndexError):
            return False
        tooltip.set_text(command)
        view.set_tooltip_row(tooltip, path)
        return True

    def process_selected(self, selection):
        _model, iterator = selection.get_selected()
        self.end_button.set_sensitive(iterator is not None)
        self.more_button.set_sensitive(iterator is not None)

    def update_actions(self, items):
        """Enables the actions that make sense: a running process can be stopped, a stopped one can continue."""
        model, iterator = self.process_selection.get_selected()
        stopped = iterator is not None and model[iterator][7] == "T"
        for action, item in items.items():
            if action == "continue":
                item.set_sensitive(stopped)
            elif action == "stop":
                item.set_sensitive(iterator is not None and not stopped)
            else:
                item.set_sensitive(iterator is not None)

    def signal_selected(self, action):
        """Applies an action to the selected process. Asks first if work could be lost."""
        model, iterator = self.process_selection.get_selected()
        if iterator is None:
            return
        pid, start = model[iterator][1], model[iterator][5]
        self.confirm(action, model[iterator][0], lambda: self.send_signal(action, pid, start))

    def send_signal(self, action, pid, start):
        """Sends the signal of an action to a process, unless the selection changed in the meantime."""
        try:
            self.signal_process(pid, start, ACTIONS[action][0])
            if action in ("end", "kill"):
                self.signaled[(pid, start)] = action
        except (OSError, ValueError) as error:
            self.show_error(ACTIONS[action][5], str(error))
        self.refresh()

    def features(self):
        """Returns what the scan has to read for the visible columns, besides the basics."""
        wanted = set()
        for key in ("command", "swap"):
            if self.columns[key].get_visible():
                wanted.add(key)
        if any(self.columns[key].get_visible() for key in UNIT_KEYS):
            wanted.add("unit")
        if any(self.columns[key].get_visible() for key in IO_KEYS):
            wanted.add("io")
        if any(self.columns[key].get_visible() for key in GPU_KEYS):
            wanted.add("gpu")
        return wanted

    def refresh(self):
        self.scanner.request(self.features(), self.on_scanned)

    def on_scanned(self, processes):
        """Called with the new list of processes: updates the rows."""
        rows = {}
        for p in processes:
            rows[p.key] = process_row(p, self.icons.icon_for(p))
        # Forget the processes that are gone
        self.signaled = {key: action for key, action in self.signaled.items() if key in rows}
        # In the tree, a process goes under its parent. Without a parent, or if the parent is gone, it is at the top.
        by_pid = {key[0]: key for key in rows}
        desired = {key: (by_pid.get(row[25]) if self.show_tree and by_pid.get(row[25]) != key else None)
                   for key, row in rows.items()}
        self.sync_rows(rows, desired)
        if self.expand_when_filled and rows:
            self.expand_when_filled = False
            self.view.expand_all()
