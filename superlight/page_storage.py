import copy
import gettext
import os
from functools import partial

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk
from xapp.threading import run_async, run_idle

from superlight import MARGIN, _, format_size, rgb
from superlight.chunks.cache import Cache
from superlight.chunks.flatpak import Flatpak
from superlight.chunks.folder import Folder
from superlight.chunks.hidden_files import Hidden
from superlight.chunks.logs import Logs
from superlight.chunks.other import Other
from superlight.chunks.personal_files import Personal
from superlight.chunks.snapshots import Snapshots
from superlight.chunks.steam import Steam
from superlight.chunks.swap import Swap
from superlight.chunks.system_cache import SystemCache
from superlight.chunks.temp_files import Temp
from superlight.chunks.trash import Trash
from superlight.common_disk import FolderScanner, Measurer, existing, list_drives, mount_of, read_mounts
from superlight.view_folders import FolderView


# Colors for the rows, from the biggest to the smallest: the Tableau palette, which is readable on light and dark
# themes, with three more
FOLDER_COLORS = (
    "#4E79A7", "#F28E2B", "#59A14F", "#B07AA1", "#E15759", "#76B7B2", "#EDC948", "#9C755F", "#FF9DA7",
    "#6B6ECF", "#8CA252", "#7B4173")
FOLDERS_SHOWN = 8


class ShareBar(Gtk.DrawingArea):
    """A horizontal bar split in parts, each with a color and a share of the whole. The rest is free space."""

    def __init__(self, height):
        super().__init__()
        self.parts = []
        self.set_size_request(-1, height)
        self.connect("draw", self.on_draw)

    def set_parts(self, parts):
        self.parts = parts
        self.queue_draw()

    def on_draw(self, _widget, cr):
        width, height = self.get_allocated_width(), self.get_allocated_height()
        color = self.get_style_context().get_color(self.get_state_flags())
        radius = min(4, height / 2)

        def rounded(x, w):
            cr.new_sub_path()
            for cx, cy, start in ((x + w - radius, radius, -90), (x + w - radius, height - radius, 0),
                                  (x + radius, height - radius, 90), (x + radius, radius, 180)):
                cr.arc(cx, cy, radius, start * 3.14159265 / 180, (start + 90) * 3.14159265 / 180)
            cr.close_path()

        # The background of the bar
        rounded(0, width)
        cr.set_source_rgba(color.red, color.green, color.blue, 0.10)
        cr.fill()
        # Draw the parts inside the bar, with a gap of one pixel between them
        rounded(0, width)
        cr.clip()
        x = 0.0
        for fraction, part_color in self.parts:
            part = max(0.0, fraction) * width
            if part >= 0.5:
                cr.rectangle(x, 0, max(0.0, part - 1), height)
                cr.set_source_rgb(*rgb(part_color))
                cr.fill()
            x += part
        return False


class StoragePage(Gtk.Stack):

    def __init__(self, window):
        super().__init__()
        self.window = window
        self.overview = Gtk.ScrolledWindow()
        self.overview.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.add_named(self.overview, "overview")
        self.explorer = FolderView(window, self.leave_explorer)
        self.add_named(self.explorer, "explorer")
        self.views = {}           # the view of each chunk that has one, by key. Created the first time it is needed.
        self.drives = []
        self.drive = None
        self.chunks = []
        self.sizes = {}           # the size of each path, when it is known
        self.measuring = set()    # the paths that are being measured now
        self.home = GLib.get_home_dir()
        self.measurer = Measurer(self.on_measured)
        self.measured_once = False
        self.root_scans = {}      # the biggest folders at the top of a drive, by mount. For drives without the home folder.

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=MARGIN)
        top = Gtk.Box(spacing=12)
        self.drive_combo = Gtk.ComboBoxText()
        self.drive_combo.connect("changed", self.on_drive_changed)
        top.pack_start(self.drive_combo, False, False, 0)
        self.spinner = Gtk.Spinner()
        top.pack_start(self.spinner, False, False, 0)
        self.status = Gtk.Label(xalign=0)
        self.status.get_style_context().add_class("dim-label")
        top.pack_start(self.status, True, True, 0)
        self.refresh_button = Gtk.Button.new_from_icon_name("xsi-view-refresh-symbolic", Gtk.IconSize.BUTTON)
        self.refresh_button.set_tooltip_text(_("Measure again"))
        self.refresh_button.connect("clicked", lambda *_: self.measure(force=True))
        top.pack_end(self.refresh_button, False, False, 0)
        page.pack_start(top, False, False, 0)

        numbers = Gtk.Box()
        self.used_label = Gtk.Label(xalign=0)
        self.free_label = Gtk.Label(xalign=1)
        self.free_label.get_style_context().add_class("dim-label")
        numbers.pack_start(self.used_label, True, True, 0)
        numbers.pack_end(self.free_label, False, False, 0)
        page.pack_start(numbers, False, False, 0)
        self.bar = ShareBar(22)
        page.pack_start(self.bar, False, False, 0)
        self.list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, activate_on_single_click=True)
        self.list_box.get_style_context().add_class("frame")
        self.list_box.connect("row-activated", self.on_chunk_activated)
        table = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=MARGIN)
        page.pack_start(table, False, False, 0)
        table.pack_start(self.list_box, False, False, 0)
        # At the bottom: the cleanup buttons on the left, the buttons to explore the folders on the right
        bottom = Gtk.Box(spacing=MARGIN)
        self.cleanup_box = Gtk.Box(spacing=MARGIN)
        bottom.pack_start(self.cleanup_box, False, False, 0)
        self.explore_box = Gtk.Box(spacing=MARGIN)
        bottom.pack_end(self.explore_box, False, False, 0)
        table.pack_start(bottom, False, False, 0)
        self.note = Gtk.Label(xalign=0, wrap=True)
        self.note.get_style_context().add_class("dim-label")
        page.pack_start(self.note, False, False, 0)
        self.overview.add(page)

    def refresh(self):
        """Called when the page is shown: updates the list of drives, and measures the first time."""
        chosen = self.drive.mount if self.drive else None
        self.drives = list_drives()
        self.drive_combo.handler_block_by_func(self.on_drive_changed)
        self.drive_combo.remove_all()
        for drive in self.drives:
            self.drive_combo.append(drive.mount, "%s (%s)" % (drive.name, format_size(drive.total)))
        self.drive_combo.handler_unblock_by_func(self.on_drive_changed)
        self.drive_combo.set_visible(len(self.drives) > 1)
        if self.drives:
            self.drive_combo.set_active_id(chosen if any(drive.mount == chosen for drive in self.drives) else self.drives[0].mount)
        if not self.measured_once:
            self.measured_once = True
            self.measure()

    def on_drive_changed(self, combo):
        self.drive = next((drive for drive in self.drives if drive.mount == combo.get_active_id()), None)
        self.show_drive()

    def drive_of(self, path, mounts):
        """Returns the drive that a path is on."""
        mount = mount_of(path, mounts)
        return next((drive for drive in self.drives if mount is not None and drive.device == mount.device), None)

    def measure(self, force=False):
        """Measures the folders of the chunks and the home folder."""
        if force:
            for scan in self.root_scans.values():
                scan["scanner"].stop()
            self.root_scans = {}
            self.measurer.stop()
            self.measurer = Measurer(self.on_measured)
            self.sizes = {}
            self.measuring = set()
        # The order matters. Each chunk is told which paths the previous ones took and leaves them out. Personal files
        # and Hidden files come last because they take what is left of the home folder. The buttons use this order too.
        self.chunks = []
        taken = []
        for chunk_class in (Steam, Flatpak, Trash, Cache, Snapshots, Personal, SystemCache, Logs, Swap, Temp, Hidden):
            chunk = chunk_class()
            found = chunk.detect(taken)
            taken += found
            chunk.paths = existing(found)
            self.chunks.append(chunk)
        paths = [path for chunk in self.chunks for path in chunk.paths]
        if os.path.isdir(self.home):
            paths.append(self.home)
        # A file has its size already, no need to measure it
        for path in list(paths):
            if os.path.isfile(path) and not os.path.isdir(path):
                self.sizes[path] = os.lstat(path).st_blocks * 512
        self.measuring = set(path for path in paths if path not in self.sizes)
        self.measurer.measure(sorted(self.measuring))
        self.show_drive()

    def on_measured(self, path, size):
        self.measuring.discard(path)
        if size is not None:
            self.sizes[path] = size
        self.show_drive()
        return GLib.SOURCE_REMOVE

    def attributed(self):
        """Returns what each chunk takes on the drive that is shown, and what the rest of the home folder takes (None if
        not known yet), as ({key: bytes}, home_other)."""
        mounts = read_mounts()
        shown = {}
        under_home = 0
        for chunk in self.chunks:
            total = 0
            for path in chunk.paths:
                size = self.sizes.get(path)
                drive = self.drive_of(path, mounts)
                if size is not None and drive is not None and drive.mount == self.drive.mount:
                    total += size
                    if path.startswith(self.home.rstrip("/") + "/"):
                        under_home += size
            if total > 0:
                shown[chunk.key] = total
        home_drive = self.drive_of(self.home, mounts)
        home_total = self.sizes.get(self.home)
        home_other = None
        if home_total is not None and home_drive is not None and home_drive.mount == self.drive.mount:
            home_other = max(0, home_total - under_home)
        return shown, home_other

    def show_drive(self):
        if self.drive is None:
            return
        drive = self.drive
        self.used_label.set_markup("<b>%s</b> %s" % (GLib.markup_escape_text(format_size(drive.used)),
                                                      GLib.markup_escape_text(_("used of %s") % format_size(drive.total))))
        self.free_label.set_text(_("%s free") % format_size(drive.free))
        reserved = max(0, drive.total - drive.used - drive.free)
        self.free_label.set_tooltip_text(
            _("%s are kept by the file system for the system and can't be used.") % format_size(reserved) if reserved > 0 else None)
        shown, home_other = self.attributed()
        entries = [(chunk, shown[chunk.key]) for chunk in self.chunks if chunk.key in shown]
        if home_other:
            entries.append((next(chunk for chunk in self.chunks if chunk.key == "hidden"), home_other))
        entries.sort(key=lambda entry: -entry[1])
        # A drive that doesn't have the home folder also shows its biggest folders
        scanning = False
        home_drive = self.drive_of(self.home, read_mounts())
        if home_drive is None or home_drive.mount != drive.mount:
            taken = [os.path.realpath(path) for chunk in self.chunks for path in chunk.paths]
            extra, scanning = self.drive_folders(drive, taken)
            entries = sorted(entries + extra, key=lambda entry: -entry[1])
        entries = sorted((entry for entry in entries
                          if entry[0].get_display_size(entry[1], self.paths_here(entry[0])) is not None),
                         key=lambda entry: -entry[1])
        # The colors are given in order of size
        colored = []
        for number, (chunk, size) in enumerate(entries):
            chunk = copy.copy(chunk)
            chunk.color = FOLDER_COLORS[number % len(FOLDER_COLORS)]
            colored.append((chunk, size))
        entries = colored
        known = sum(size for _chunk, size in entries)
        # What is used and not attributed to a row: the system, files outside the home folder, files that can't be read
        rest = max(0, drive.used - known) if not self.measuring and not scanning else None
        parts = [(size / drive.total, chunk.color) for chunk, size in entries]
        if rest:
            parts.append((rest / drive.total, Other.color))
        self.bar.set_parts(parts)
        # The table has a row for it, like the bar
        listed = list(entries)
        if rest:
            listed.append((Other(), rest))
            listed = sorted((entry for entry in listed
                             if entry[0].get_display_size(entry[1], self.paths_here(entry[0])) is not None),
                            key=lambda entry: -entry[1])
        # Some rows are always listed, even when they are empty (the trash, to be able to open it). The home drive has a
        # trash even if there is no trash folder yet.
        for chunk in self.chunks:
            if chunk.always_listed and all(chunk.key != listed_chunk.key for listed_chunk, _size in listed) and (
                    self.paths_here(chunk) or (home_drive is not None and home_drive.mount == drive.mount)):
                listed.append((chunk, shown.get(chunk.key, 0)))
        self.show_list(listed, drive)
        self.show_explore_buttons(drive, read_mounts())
        self.show_cleanup()
        busy = bool(self.measuring) or scanning
        self.spinner.set_visible(busy)
        self.spinner.set_property("active", busy)
        self.status.set_text(_("Measuring…") if busy else "")
        if not entries and not busy:
            self.note.set_text(_("There is nothing on this drive."))
        else:
            self.note.set_text("")
        self.note.set_visible(bool(self.note.get_text()))

    def drive_folders(self, drive, taken):
        """Returns the biggest items in the top folder of a drive, except the paths in taken, as a list of
        (chunk, size) and whether the scan is still running."""
        scan = self.root_scans.get(drive.mount)
        if scan is None:
            scan = {"entries": [], "done": False}
            scan["scanner"] = FolderScanner(drive.mount, partial(self.on_root_entries, scan),
                                            partial(self.on_root_done, scan))
            self.root_scans[drive.mount] = scan
        biggest = sorted((entry for entry in scan["entries"] if entry[3] > 0
                          and not any(os.path.realpath(entry[0]) == path or os.path.realpath(entry[0]).startswith(path + "/")
                                      or path.startswith(os.path.realpath(entry[0]) + "/") for path in taken)),
                         key=lambda entry: -entry[3])[:FOLDERS_SHOWN]
        entries = []
        for path, name, is_folder, size, _modified, _readable in biggest:
            entries.append((Folder(path, name, is_folder), size))
        return entries, not scan["done"]

    def on_root_entries(self, scan, batch):
        scan["entries"].extend(batch)
        self.show_drive()
        return GLib.SOURCE_REMOVE

    def on_root_done(self, scan, _total):
        scan["done"] = True
        self.show_drive()
        return GLib.SOURCE_REMOVE

    def show_list(self, entries, drive):
        for row in self.list_box.get_children():
            self.list_box.remove(row)
        biggest = max((size for _chunk, size in entries), default=1) or 1
        for chunk, size in entries:
            box = Gtk.Box(spacing=12, margin=8)
            box.pack_start(Gtk.Image.new_from_icon_name(chunk.icon, Gtk.IconSize.MENU), False, False, 0)
            name = Gtk.Label(label=chunk.name, xalign=0)
            box.pack_start(name, True, True, 0)
            bar = ShareBar(8)
            bar.set_size_request(150, 8)
            bar.set_valign(Gtk.Align.CENTER)
            bar.set_parts([(size / biggest, chunk.color)])
            box.pack_start(bar, False, False, 0)
            amount = Gtk.Label(label=chunk.get_display_size(size, self.paths_here(chunk)), xalign=1, width_chars=9)
            box.pack_start(amount, False, False, 0)
            self.list_box.add(box)
            box.get_parent().chunk = chunk
        self.list_box.set_visible(bool(entries))
        self.list_box.show_all()

    def show_cleanup(self):
        """Shows a button for each chunk that can free space."""
        for child in self.cleanup_box.get_children():
            self.cleanup_box.remove(child)
        for chunk in self.chunks:
            paths = self.paths_here(chunk)
            cleanup = chunk.get_cleanup_action(sum(self.sizes.get(path, 0) for path in paths), paths)
            if cleanup:
                button = Gtk.Button(label=cleanup.label)
                button.connect("clicked", self.on_cleanup_clicked, chunk, cleanup)
                self.cleanup_box.pack_start(button, False, False, 0)
        self.cleanup_box.show_all()

    def on_cleanup_clicked(self, _button, chunk, cleanup):
        self.confirm(cleanup.question, cleanup.warning, cleanup.button, lambda: self.clean(chunk, cleanup.action))

    def confirm(self, question, warning, button, action):
        dialog = Gtk.MessageDialog(transient_for=self.window, modal=True, message_type=Gtk.MessageType.QUESTION,
                                   buttons=Gtk.ButtonsType.NONE, text=question)
        dialog.format_secondary_text(warning)
        dialog.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
        confirm = dialog.add_button(button, Gtk.ResponseType.ACCEPT)
        confirm.get_style_context().add_class("destructive-action")
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)
        dialog.get_widget_for_response(Gtk.ResponseType.ACCEPT).grab_focus()
        dialog.connect("response", self.on_confirmed, action)
        dialog.present()

    def on_confirmed(self, dialog, response, action):
        dialog.destroy()
        if response == Gtk.ResponseType.ACCEPT:
            action()

    def paths_here(self, chunk):
        """Returns the paths of a chunk that are on the drive that is shown."""
        if self.drive is None:
            return []
        mounts = read_mounts()
        here = []
        for path in chunk.paths:
            drive = self.drive_of(path, mounts)
            if drive is not None and drive.mount == self.drive.mount:
                here.append(path)
        return here

    def clean(self, chunk, action):
        """Runs a cleanup in a thread, then measures again."""
        self.work(action, chunk)

    @run_async
    def work(self, action, chunk):
        failures = action()
        self.cleaned(chunk, failures)

    @run_idle
    def cleaned(self, chunk, failures):
        paths = chunk.paths
        for path in paths:
            self.sizes.pop(path, None)
        self.measuring.update(paths)
        self.measurer.measure(paths)
        self.show_drive()
        if failures:
            dialog = Gtk.MessageDialog(transient_for=self.window, modal=True, message_type=Gtk.MessageType.WARNING,
                                       buttons=Gtk.ButtonsType.CLOSE, text=_("Some of it could not be deleted"))
            dialog.format_secondary_text(gettext.ngettext("%d item is still there. It may be in use, or not yours.",
                                                         "%d items are still there. They may be in use, or not yours.", failures) % failures)
            dialog.connect("response", lambda dialog, _response: dialog.destroy())
            dialog.present()

    def show_explore_buttons(self, drive, mounts):
        """Shows the buttons to explore the drive: the home folder, if it is on this drive, and the drive itself."""
        for button in self.explore_box.get_children():
            self.explore_box.remove(button)
        home_drive = self.drive_of(self.home, mounts)
        entries = []
        if home_drive is not None and home_drive.mount == drive.mount:
            entries.append((_("Explore Home Folder…"), self.home))
        # Skip this button if the drive is the home folder itself
        if not entries or os.path.realpath(drive.mount) != os.path.realpath(self.home):
            entries.append((_("Explore File System…") if drive.mount == "/" else _("Explore Folders…"), drive.mount))
        for label, path in entries:
            button = Gtk.Button(label=label)
            button.connect("clicked", self.on_explore_clicked, path)
            self.explore_box.pack_start(button, False, False, 0)
        self.explore_box.show_all()

    def on_explore_clicked(self, _button, path):
        self.open_folder(path)

    def on_chunk_activated(self, _list, row):
        row.chunk.click(self)

    def show_view(self, chunk):
        """Shows the view of a chunk."""
        view = self.views.get(chunk.key)
        if view is None:
            view = chunk.create_view(self.window, self.leave_view)
            self.views[chunk.key] = view
            self.add_named(view, chunk.key)
            view.show_all()
        view.load()
        self.set_visible_child(view)

    def leave_view(self):
        self.set_visible_child_name("overview")

    def browse(self, chunk, show_hidden=True, exclude_paths=()):
        """Shows the folders of a chunk, starting at its biggest path on the drive that is shown. Hidden files can be
        left out, and so can the paths in exclude_paths."""
        folder = chunk.root
        here = self.paths_here(chunk)
        if not folder and here:
            folder = max(here, key=lambda path: self.sizes.get(path, 0))
        if folder:
            self.open_folder(folder, show_hidden, {path: self.sizes.get(path) for path in existing(exclude_paths)})

    def open_folder(self, path, show_hidden=True, excluded=None):
        self.explorer.start(path, show_hidden, excluded)
        self.set_visible_child_name("explorer")

    def leave_explorer(self, changed):
        """Called when going back from the explorer. If something was moved to the trash, measures again."""
        self.set_visible_child_name("overview")
        if changed:
            self.measure(force=True)

    def stop(self):
        self.measurer.stop()
        for scan in self.root_scans.values():
            scan["scanner"].stop()
        self.explorer.stop()
        for view in self.views.values():
            view.stop()
