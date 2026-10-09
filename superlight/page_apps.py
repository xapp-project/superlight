import os
import re
import signal
from collections import namedtuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, GObject, Gtk, Pango

from superlight import MARGIN, _, format_value
from superlight.common_proc import process_stat
from superlight.common_process import ProcessTable


# The columns, as (key used in the settings, title, column number in the store, kind of value, shown by default)
COLUMNS = (("name", _("Name"), 2, "text", True),
           ("processes", _("Processes"), 3, "number", True),
           ("started", _("Started"), 16, "time", False),
           ("cpu", _("CPU"), 4, "percent", True),
           ("cpu-time", _("CPU Time"), 10, "duration", False),
           ("memory", _("Memory"), 5, "size", True),
           ("swap", _("Swap"), 9, "total", False),
           ("disk-read", _("Disk Read"), 6, "rate", False),
           ("disk-write", _("Disk Write"), 7, "rate", False),
           ("disk-read-total", _("Disk Read Total"), 11, "total", False),
           ("disk-write-total", _("Disk Write Total"), 12, "total", False),
           ("gpu", _("GPU"), 8, "gpu", False),
           ("video-memory", _("Video Memory"), 13, "total", False),
           ("video-encoder", _("Video Encoder"), 14, "gpu", False),
           ("video-decoder", _("Video Decoder"), 15, "gpu", False))
# In the menu of the columns, a separator is added after each of these
GROUP_ENDS = ("started", "cpu-time", "swap", "disk-write-total")
KEYS = tuple(column[0] for column in COLUMNS)
DEFAULT_VISIBLE = tuple(column[0] for column in COLUMNS if column[4])
STORE_COLUMNS = {column[0]: column[2] for column in COLUMNS}
IO_KEYS = ("disk-read", "disk-write", "disk-read-total", "disk-write-total")
GPU_KEYS = ("gpu", "video-memory", "video-encoder", "video-decoder")
# Programs that only start other programs. Their name tells nothing about which application a process belongs to
GENERIC_PROGRAMS = {"env", "sh", "bash", "dash", "zsh", "fish", "python", "python3", "perl", "ruby", "node", "java",
                    "flatpak", "snap", "gtk-launch", "gio", "xdg-open", "sudo", "pkexec", "wine", "electron"}
# The names of systemd units for applications: app-<launcher>-<id>-<number>.scope, or for an application that
# starts at login: app-<id>@autostart.service
UNIT = re.compile(r"^app-(?P<stem>.+?)(?:@[^.]*\.service|-\d+\.(?:scope|service))$")
# The actions, as (signal, menu entry, question, warning, button of the question, title of the error). The question
# is None for actions that don't ask first.
ACTIONS = {"end": (signal.SIGTERM, _("End Application"), _('End "%s"?'),
                   _("All its processes will be asked to quit. They may lose unsaved work."), _("End Application"),
                   _("Could not end the application")),
           "kill": (signal.SIGKILL, _("Kill Application"), _('Kill "%s"?'),
                    _("All its processes will stop immediately. Unsaved work may be lost."), _("Kill Application"),
                    _("Could not kill the application")),
           "stop": (signal.SIGSTOP, _("Stop Application"), None, None, None, _("Could not stop the application")),
           "continue": (signal.SIGCONT, _("Continue Application"), None, None, None,
                        _("Could not continue the application"))}

App = namedtuple("App", ("id", "name", "icon"))


class Catalog:
    """The installed applications. It finds which application a process or a systemd unit belongs to."""

    def __init__(self):
        self.by_id = {}
        self.by_program = {}
        self.units = {}
        for info in Gio.AppInfo.get_all():
            identifier = (info.get_id() or "").removesuffix(".desktop")
            if not identifier or not info.should_show():
                continue
            app = App(identifier, info.get_display_name(), info.get_icon())
            self.by_id[identifier.lower()] = app
            executable = info.get_executable()
            program = os.path.basename(executable).lower() if executable else ""
            if program and program not in GENERIC_PROGRAMS:
                self.by_program.setdefault(program, app)

    def from_unit(self, unit):
        """Returns the application that was started in this systemd unit, or None."""
        if unit not in self.units:
            app = None
            match = UNIT.match(re.sub(r"\\x([0-9a-f]{2})", lambda m: chr(int(m.group(1), 16)), unit))
            if match:
                stem = match.group("stem").lower()
                app = self.by_id.get(stem)
                if app is None:   # the unit name can have a launcher name before the id
                    app = next((app for identifier, app in self.by_id.items() if stem.endswith("-" + identifier)), None)
            self.units[unit] = app
        return self.units[unit]

    def from_process(self, process):
        """Returns the application that this process is the program of, or None."""
        command = process.command.split(" ", 1)[0] if process.command else ""
        for program in (os.path.basename(command), process.executable, process.name):
            app = self.by_program.get(program.lower())
            if app is not None:
                return app
        return None


def group_apps(processes, user, catalog):
    """Returns the running applications of a user, with their processes, as {id: (app, [process, ..])}.

    A process belongs to an application if it runs in the unit of the application, if it is the program of the
    application, or if its parent process belongs to the application."""
    mine = {process.key: process for process in processes if process.user == user}
    by_pid = {process.pid: process for process in mine.values()}
    known = {}

    def app_of(process):
        if process.key in known:
            return known[process.key]
        known[process.key] = None   # stops the search from going around in circles
        app = catalog.from_unit(process.unit) if process.unit else None
        app = app or catalog.from_process(process)
        if app is None:
            parent = by_pid.get(process.parent)
            if parent is not None and parent is not process:
                app = app_of(parent)
        known[process.key] = app
        return app

    groups = {}
    for process in mine.values():
        app = app_of(process)
        if app is not None:
            groups.setdefault(app.id, (app, []))[1].append(process)
    return groups


def total(values):
    """Returns the sum of the values that are known (not -1), or -1 if none is."""
    known = [value for value in values if value >= 0]
    return sum(known) if known else -1


class AppsPage(ProcessTable, Gtk.Box):

    PREFIX = "apps"
    COLUMNS = COLUMNS
    KEYS = KEYS
    GROUP_ENDS = GROUP_ENDS
    DEFAULT_VISIBLE = DEFAULT_VISIBLE
    STORE_COLUMNS = STORE_COLUMNS
    ACTIONS = ACTIONS
    STOP_TOOLTIP = _("Pause all its processes, until they are continued")

    def __init__(self, window, settings, scanner, catalog):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0, border_width=MARGIN)
        self.window = window
        self.settings = settings
        self.scanner = scanner
        self.catalog = catalog
        self.user = GLib.get_user_name()
        self.members = {}   # the processes of each application, as (pid, start)
        self.signaled = set()   # the applications that were asked to end or were killed, and are still running
        self.search_text = ""
        # id, icon, name, number of processes, CPU, memory, disk read, disk write, GPU, swap, CPU time, disk read
        # total, disk write total, video memory, video encoder, video decoder, start time, number of stopped processes
        self.store = Gtk.ListStore(str, Gio.Icon, str, int, float, GObject.TYPE_UINT64, float, float, float,
                                   GObject.TYPE_INT64, float, GObject.TYPE_INT64, GObject.TYPE_INT64,
                                   GObject.TYPE_INT64, float, float, GObject.TYPE_UINT64, int)
        self.filter = self.store.filter_new()
        self.filter.set_visible_func(self.matches_search)
        self.sorted_store = Gtk.TreeModelSort(model=self.filter)
        view = Gtk.TreeView(model=self.sorted_store, headers_visible=True)
        view.set_enable_search(False)
        view.connect("button-press-event", self.on_view_pressed)
        self.columns = {}
        for key, title, number, kind, _default in COLUMNS:
            renderer = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END, width_chars=12)
            column = Gtk.TreeViewColumn(title)
            if key == "name":
                icon_renderer = Gtk.CellRendererPixbuf()
                icon_renderer.set_property("stock-size", Gtk.IconSize.MENU)
                column.pack_start(icon_renderer, False)
                column.add_attribute(icon_renderer, "gicon", 1)
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
        self.restore_columns()
        view.connect("columns-changed", self.on_columns_changed)
        sort_column = self.settings.get_string("apps-sort-column")
        self.sorted_store.set_sort_column_id(
            STORE_COLUMNS.get(sort_column, STORE_COLUMNS["memory"]),
            Gtk.SortType.ASCENDING if self.settings.get_boolean("apps-sort-order-ascending") else Gtk.SortType.DESCENDING)
        self.sorted_store.connect("sort-column-changed", self.on_sort_changed)
        self.selection = view.get_selection()
        self.selection.connect("changed", self.on_selected)
        scroll = Gtk.ScrolledWindow()
        scroll.set_overlay_scrolling(False)
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_shadow_type(Gtk.ShadowType.IN)
        scroll.add(view)
        self.pack_start(scroll, True, True, 0)

        bar = Gtk.Box(spacing=MARGIN, margin_top=MARGIN)
        self.search_entry = Gtk.SearchEntry(placeholder_text=_("Search"), width_chars=24)
        # Typing in the list goes to the search entry
        view.connect("key-press-event", lambda _view, event: self.search_entry.handle_event(event))
        self.search_entry.connect("search-changed", self.on_search_changed)
        self.search_entry.connect("stop-search", self.on_search_stopped)
        bar.pack_start(self.search_entry, True, True, 0)
        self.end_button = Gtk.Button(label=_("End Application"), sensitive=False)
        self.end_button.connect("clicked", lambda *_: self.signal_selected("end"))
        self.end_button.get_style_context().add_class("destructive-action")
        menu, self.action_items = self.actions_menu(("kill", "stop", "continue"))
        menu.connect("show", lambda *_: self.update_actions(self.action_items))
        self.more_button = Gtk.MenuButton(popup=menu, sensitive=False, tooltip_text=_("More actions"))
        self.more_button.set_image(Gtk.Image.new_from_icon_name("xsi-pan-down-symbolic", Gtk.IconSize.BUTTON))
        self.more_button.get_style_context().add_class("destructive-action")
        split = Gtk.Box()
        split.get_style_context().add_class("linked")
        split.pack_start(self.end_button, False, False, 0)
        split.pack_start(self.more_button, False, False, 0)
        bar.pack_end(split, False, False, 0)
        self.pack_start(bar, False, False, 0)

    @staticmethod
    def is_paused(model, iterator):
        return model.get_value(iterator, 17) == model.get_value(iterator, 3)   # all its processes are stopped

    def is_active(self, model, iterator):
        """Returns True if the application is running normally: not paused, and not asked to end."""
        return not self.is_paused(model, iterator) and model.get_value(iterator, 0) not in self.signaled

    def style_icon(self, _column, renderer, model, iterator, _data=None):
        renderer.set_property("sensitive", self.is_active(model, iterator))

    def format_cell(self, _column, renderer, model, iterator, data):
        number, kind = data
        # Paused: gray and italic. Asked to end and still running: crossed out
        renderer.set_property("sensitive", self.is_active(model, iterator))
        renderer.set_property("style", Pango.Style.ITALIC if self.is_paused(model, iterator) else Pango.Style.NORMAL)
        renderer.set_property("strikethrough", model.get_value(iterator, 0) in self.signaled)
        renderer.set_property("text", format_value(kind, model.get_value(iterator, number)))

    def matches_search(self, model, iterator, _data=None):
        """Returns True if the application matches the search text (or if nothing is searched)."""
        return not self.search_text or self.search_text in model[iterator][2].casefold()

    def on_search_changed(self, entry):
        self.search_text = entry.get_text().strip().casefold()
        self.filter.refilter()

    def on_selected(self, selection):
        selected = selection.get_selected()[1] is not None
        self.end_button.set_sensitive(selected)
        self.more_button.set_sensitive(selected)

    def features(self):
        """Returns what the scan has to read: the unit and the command line (to find the application of each
        process), and what the visible columns need."""
        wanted = {"unit", "command"}
        if self.columns["swap"].get_visible():
            wanted.add("swap")
        if any(self.columns[key].get_visible() for key in IO_KEYS):
            wanted.add("io")
        if any(self.columns[key].get_visible() for key in GPU_KEYS):
            wanted.add("gpu")
        return wanted

    def refresh(self):
        self.scanner.request(self.features(), self.on_scanned)

    def on_scanned(self, processes):
        self.update(group_apps(processes, self.user, self.catalog))

    def update(self, groups):
        """Updates the rows of the list in place, so that the selection stays."""
        rows = {}
        self.members = {}
        for identifier, (app, members) in groups.items():
            rows[identifier] = (
                identifier, app.icon or Gio.ThemedIcon.new("application-x-executable"), app.name, len(members),
                sum(process.cpu for process in members), sum(process.memory for process in members),
                total(process.disk_read for process in members), total(process.disk_write for process in members),
                min(100.0, total(process.gpu for process in members)), sum(process.swap for process in members),
                sum(process.cpu_time for process in members), total(process.disk_read_total for process in members),
                total(process.disk_write_total for process in members),
                total(process.video_memory for process in members),
                min(100.0, total(process.video_encoder for process in members)),
                min(100.0, total(process.video_decoder for process in members)),
                min(process.started for process in members),
                sum(1 for process in members if process.state in ("T", "t")))
            self.members[identifier] = [(process.pid, process.start) for process in members]
        self.signaled &= set(rows)   # forget the applications that are gone
        for row in [row for row in self.store if row[0] not in rows]:
            self.store.remove(row.iter)
        shown = set()
        for row in self.store:
            shown.add(row[0])
            self.store.set(row.iter, list(range(18)), rows[row[0]])
        for identifier, values in rows.items():
            if identifier not in shown:
                self.store.append(values)

    def update_actions(self, items):
        """Enables the actions that make sense: a running application can be stopped, a stopped one can continue."""
        model, iterator = self.selection.get_selected()
        if iterator is None:
            return
        stopped, count = model[iterator][17], model[iterator][3]
        for action, item in items.items():
            if action == "continue":
                item.set_sensitive(stopped > 0)
            elif action == "stop":
                item.set_sensitive(stopped < count)

    def signal_selected(self, action):
        """Applies an action to all the processes of the selected application. Asks first if work could be lost."""
        model, iterator = self.selection.get_selected()
        if iterator is None:
            return
        _signal, _title, question, warning, button, _error = ACTIONS[action]
        key = model[iterator][0]
        if question is None:
            self.send_signal(action, list(self.members.get(key, ())), key)
            return
        confirmation = Gtk.MessageDialog(transient_for=self.window, modal=True,
                                         message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.NONE,
                                         text=question % model[iterator][2])
        confirmation.format_secondary_text(warning)
        confirmation.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
        confirm = confirmation.add_button(button, Gtk.ResponseType.ACCEPT)
        confirm.get_style_context().add_class("destructive-action")
        confirmation.set_default_response(Gtk.ResponseType.ACCEPT)
        confirmation.get_widget_for_response(Gtk.ResponseType.ACCEPT).grab_focus()
        confirmation.connect("response", self.on_signal_response, action, list(self.members.get(key, ())), key)
        confirmation.present()

    def on_signal_response(self, dialog, response, action, members, key):
        dialog.destroy()
        if response == Gtk.ResponseType.ACCEPT:
            self.send_signal(action, members, key)

    def send_signal(self, action, members, key):
        """Sends the signal of an action to each process of an application."""
        failure = None
        sent = False
        for pid, start in members:
            descriptor = None
            try:
                descriptor = os.pidfd_open(pid)
                # Skip it if the PID now belongs to another process
                if process_stat(pid)[2] == start:
                    signal.pidfd_send_signal(descriptor, ACTIONS[action][0])
                    sent = True
            except ProcessLookupError:
                continue
            except (OSError, ValueError) as error:
                failure = failure or error
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        if sent and action in ("end", "kill"):
            self.signaled.add(key)
            self.view.queue_draw()
        if failure is not None:
            error_dialog = Gtk.MessageDialog(transient_for=self.window, modal=True,
                                             message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE,
                                             text=ACTIONS[action][5])
            error_dialog.format_secondary_text(str(failure))
            error_dialog.connect("response", lambda dialog, _response: dialog.destroy())
            error_dialog.present()
        self.refresh()
