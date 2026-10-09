import os
from collections import namedtuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk
from xapp.threading import run_async, run_idle

from superlight import _
from superlight.common_proc import boot_time, process_stat, username

# Everything we know about a process. Rates are measured since the previous scan. -1 means not known.
Process = namedtuple("Process", (
    "key", "start", "pid", "parent", "name", "executable", "user", "state", "nice", "cpu", "cpu_time", "started",
    "memory", "virtual", "resident", "shared", "swap", "command", "unit", "disk_read", "disk_write",
    "disk_read_total", "disk_write_total", "gpu", "video_memory", "video_encoder", "video_decoder"))

# The GPU engines for each kind of work. For each kind, the busiest engine is the one that counts.
GPU_ENGINES = {"gpu": ("gfx", "render", "compute"), "video-encoder": ("enc",), "video-decoder": ("dec",)}
GAP = 5_000_000   # microseconds. After this long without a scan, rates start again from zero
UNITS = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3}


class ProcessScanner:
    """Reads the list of running processes from /proc for the pages that show them.

    Only one scan runs at a time. Reading some information (command line, unit, disk I/O, swap, GPU) costs more,
    so the page says which of these it needs."""

    def __init__(self):
        self.scanning = False
        self.queued = None   # a request that came in while a scan was running
        self.last_time = None
        self.previous_ticks = {}
        self.previous_io = {}
        self.previous_engines = {}

    def request(self, features, callback):
        """Scans in a thread, then calls callback with the list of processes, in the main loop.

        features is the extra information to read: any of "command", "unit", "io", "swap" and "gpu"."""
        if self.scanning:
            self.queued = (features, callback)
            return
        self.scanning = True
        self.read(frozenset(features), callback)

    def scan_now(self, features):
        """Scans right now and returns the processes. This takes a few milliseconds."""
        now, found = self.collect(frozenset(features))
        return self.build(now, found)

    @run_async
    def read(self, features, callback):
        now, found = self.collect(features)
        self.finish(now, found, callback)

    @staticmethod
    def collect(features):
        """Reads /proc and returns the time and a tuple of values for each process. This runs in a thread."""
        now = GLib.get_monotonic_time()
        page_size = os.sysconf("SC_PAGE_SIZE")
        ticks_per_second = os.sysconf("SC_CLK_TCK")
        started_at = boot_time()
        found = []
        for entry in os.scandir("/proc"):
            if not entry.name.isdecimal():
                continue
            try:
                pid = int(entry.name)
                name, ticks, start, _rss, state, nice, virtual, parent = process_stat(pid)
                # The kernel cuts the name to 15 characters. The command line has the full name.
                try:
                    with open(f"/proc/{pid}/cmdline", "rb") as source:
                        args = [arg.decode("utf-8", errors="replace")
                                for arg in source.read().rstrip(b"\0").split(b"\0")]
                    if args and args[0]:
                        candidates = [os.path.basename(args[0])]
                        # For a script, argv[0] is the interpreter and the script is one of the arguments. Only use
                        # one whose name starts with the kernel's name
                        candidates.extend(os.path.basename(arg) for arg in args[1:]
                                          if not arg.startswith("-"))
                        for candidate in candidates:
                            if candidate.startswith(name):
                                name = candidate
                                break
                except OSError:
                    pass
                with open(f"/proc/{pid}/statm", encoding="utf-8") as source:
                    memory_fields = source.read().split()
                resident, shared = int(memory_fields[1]), int(memory_fields[2])
                try:
                    executable = os.path.basename(os.readlink(f"/proc/{pid}/exe"))
                except OSError:
                    executable = name
                swap = 0
                if "swap" in features:
                    try:
                        with open(f"/proc/{pid}/status", encoding="utf-8") as source:
                            for line in source:
                                if line.startswith("VmSwap:"):
                                    swap = int(line.split()[1]) * 1024
                                    break
                    except (OSError, ValueError, IndexError):
                        pass
                command = ""
                if "command" in features:
                    with open(f"/proc/{pid}/cmdline", "rb") as source:
                        command = " ".join(arg.decode("utf-8", errors="replace")
                                           for arg in source.read().rstrip(b"\0").split(b"\0"))
                unit = ""
                if "unit" in features:
                    with open(f"/proc/{pid}/cgroup", encoding="utf-8") as source:
                        for line in source:
                            _hierarchy, controllers, path = line.rstrip("\n").split(":", 2)
                            if controllers in ("", "name=systemd"):
                                names = [part for part in path.split("/") if part]
                                # The last .service or .scope in the path, because a service can contain other groups
                                unit = next((part for part in reversed(names) if part.endswith((".service", ".scope"))),
                                            names[-1] if names else "")
                                break
                # Only the processes of the user can be read here, the others stay at -1
                disk = (-1, -1)
                if "io" in features:
                    try:
                        with open(f"/proc/{pid}/io", encoding="utf-8") as source:
                            values = dict(line.split(": ") for line in source.read().splitlines())
                        disk = int(values["read_bytes"]), int(values["write_bytes"])
                    except (OSError, KeyError, ValueError):
                        pass
                # What the process uses of the GPUs, from its open files in /dev/dri: the time each engine was busy
                # (in nanoseconds, as {engine: time}) and the video memory in bytes. None if it can't be read.
                gpu = None
                if "gpu" in features:
                    try:
                        fds = os.listdir(f"/proc/{pid}/fd")
                    except OSError:
                        fds = None
                    if fds is not None:
                        engines = {}
                        video_memory = 0
                        clients = set()
                        for fd in fds:
                            try:
                                if not os.readlink(f"/proc/{pid}/fd/{fd}").startswith("/dev/dri/"):
                                    continue
                                with open(f"/proc/{pid}/fdinfo/{fd}", encoding="utf-8") as source:
                                    fields = dict(line.split(":", 1) for line in source.read().splitlines() if ":" in line)
                            except OSError:
                                continue
                            # The same client can have several file descriptors. Count it once
                            client = (fields.get("drm-driver"), fields.get("drm-pdev"), fields.get("drm-client-id"))
                            if client in clients:
                                continue
                            clients.add(client)
                            for key, value in fields.items():
                                try:
                                    number, _separator, unit_name = value.strip().partition(" ")
                                    if key.startswith("drm-engine-"):
                                        engines[key[len("drm-engine-"):]] = engines.get(key[len("drm-engine-"):], 0) + int(number)
                                    elif key in ("drm-memory-vram", "drm-memory-gtt"):
                                        video_memory += int(number) * UNITS.get(unit_name, 1)
                                except ValueError:
                                    continue
                        gpu = engines, video_memory
                found.append((pid, start, parent, name, executable, username(entry.stat().st_uid), state, nice, ticks,
                              ticks / ticks_per_second, started_at + int(start) // ticks_per_second,
                              max(0, resident - shared) * page_size, virtual, resident * page_size,
                              shared * page_size, swap, command, unit, disk, gpu))
            except (OSError, ValueError, IndexError):
                continue
        return now, found

    @run_idle
    def finish(self, now, found, callback):
        """Called in the main loop when a scan is done. Gives the processes to the page, then starts the queued
        request."""
        processes = self.build(now, found)
        self.scanning = False
        queued, self.queued = self.queued, None
        callback(processes)
        if queued is not None:
            self.request(*queued)

    def build(self, now, found):
        """Returns the list of Process, with the rates calculated since the previous scan."""
        if self.last_time is None or now - self.last_time > GAP:
            # There is nothing to compare with, all the rates are zero
            self.previous_ticks, self.previous_io, self.previous_engines = {}, {}, {}
            elapsed = 1.0
        else:
            elapsed = max(0.000001, (now - self.last_time) / 1_000_000)
        ticks_per_second = os.sysconf("SC_CLK_TCK")
        processors = os.cpu_count() or 1
        wall = elapsed * 1_000_000_000   # nanoseconds that passed, the most an engine can be busy
        ticks_now, io_now, engines_now = {}, {}, {}
        processes = []
        for (pid, start, parent, name, executable, user, state, nice, ticks, cpu_time, started, memory, virtual,
             resident, shared, swap, command, unit, io, gpu) in found:
            key = (pid, start)
            ticks_now[key] = ticks
            # 100% means that every processor core is busy, like in the graphs
            cpu = max(0, ticks - self.previous_ticks.get(key, ticks)) / ticks_per_second / elapsed * 100 / processors
            if io[0] >= 0:
                io_now[key] = io
                old = self.previous_io.get(key, io)
                io_rates = (max(0, io[0] - old[0]) / elapsed, max(0, io[1] - old[1]) / elapsed)
            else:
                io_rates = (-1.0, -1.0)
            if gpu is not None:
                engines, video_memory = gpu
                engines_now[key] = engines
                old = self.previous_engines.get(key, engines)
                busy = {engine: min(100.0, max(0, ns - old.get(engine, ns)) / wall * 100)
                        for engine, ns in engines.items()}
                busiest = {kind: max((busy.get(engine, 0) for engine in names), default=0)
                           for kind, names in GPU_ENGINES.items()}
                gpu_values = (busiest["gpu"], video_memory, busiest["video-encoder"], busiest["video-decoder"])
            else:
                gpu_values = (-1.0, -1, -1.0, -1.0)
            processes.append(Process(key, start, pid, parent, name, executable, user, state, nice, cpu, cpu_time,
                                     started, memory, virtual, resident, shared, swap, command, unit, *io_rates,
                                     *io, *gpu_values))
        self.previous_ticks, self.previous_io, self.previous_engines = ticks_now, io_now, engines_now
        self.last_time = now
        return processes


class ProcessTable:
    """Shared by the Processes and Apps pages. PREFIX is different for each page."""

    def set_column_order(self, order):
        previous = None
        for key in order:
            self.view.move_column_after(self.columns[key], previous)
            previous = self.columns[key]

    def restore_columns(self):
        shown = [key for key in self.settings.get_strv("%s-visible-columns" % self.PREFIX) if key in self.columns]
        if "name" not in shown:
            shown.insert(0, "name")
        for key, column in self.columns.items():
            column.set_visible(key in shown)
        self.set_column_order(shown + [key for key in self.KEYS if key not in shown])

    def save_columns(self):
        shown = [column.key for column in self.view.get_columns() if column.get_visible()]
        self.settings.set_strv("%s-visible-columns" % self.PREFIX, shown)

    def on_columns_changed(self, view):
        # When the window closes, the columns are removed one by one. Don't save that.
        if len(view.get_columns()) == len(self.KEYS):
            self.save_columns()

    def on_header_pressed(self, _button, event):
        """Show a menu to select the columns"""
        if event.button != 3:
            return False
        menu = Gtk.Menu()
        for key, title, _number, _kind, _default in self.COLUMNS:
            item = Gtk.CheckMenuItem(label=title, active=self.columns[key].get_visible(), sensitive=key != "name")
            item.connect("toggled", self.on_column_toggled, key)
            menu.append(item)
            if key in self.GROUP_ENDS:
                menu.append(Gtk.SeparatorMenuItem())
        menu.append(Gtk.SeparatorMenuItem())
        default = Gtk.MenuItem(label=_("Use Default"))
        default.connect("activate", lambda *_: self.use_default_columns())
        menu.append(default)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def on_column_toggled(self, item, key):
        column = self.columns[key]
        last = self.view.get_columns()[-1]
        if item.get_active() and not column.get_visible() and last is not column:
            self.view.move_column_after(column, last)
        column.set_visible(item.get_active())
        self.save_columns()
        self.refresh()   # a hidden column is not read, so read it now

    def use_default_columns(self):
        """Restores the default columns and their order."""
        for key, column in self.columns.items():
            column.set_visible(key in self.DEFAULT_VISIBLE)
        self.set_column_order(self.KEYS)
        self.save_columns()
        self.refresh()

    def on_sort_changed(self, store):
        column, order = store.get_sort_column_id()
        if column is not None and column >= 0:
            self.settings.set_string("%s-sort-column" % self.PREFIX,
                                     next(key for key, number in self.STORE_COLUMNS.items() if number == column))
            self.settings.set_boolean("%s-sort-order-ascending" % self.PREFIX, order == Gtk.SortType.ASCENDING)

    def on_search_stopped(self, entry):
        entry.set_text("")
        self.view.grab_focus()

    def on_view_pressed(self, view, event):
        """Context menu"""
        if event.button != 3 or event.window is not view.get_bin_window():
            return False
        found = view.get_path_at_pos(int(event.x), int(event.y))
        if found is None:
            return False
        view.set_cursor(found[0])
        menu, items = self.actions_menu(("end", "kill", "stop", "continue"))
        self.update_actions(items)
        menu.popup_at_pointer(event)
        return True

    def on_action_activated(self, _item, action):
        self.signal_selected(action)

    def actions_menu(self, actions):
        """Returns a menu of the actions, and a dict of its items by action."""
        menu = Gtk.Menu()
        items = {}
        for action in actions:
            item = Gtk.MenuItem(label=self.ACTIONS[action][1])
            if action == "stop":
                item.set_tooltip_text(self.STOP_TOOLTIP)
            item.connect("activate", self.on_action_activated, action)
            menu.append(item)
            items[action] = item
            if action == "kill":
                menu.append(Gtk.SeparatorMenuItem())
        menu.show_all()
        return menu, items
