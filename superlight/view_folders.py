import gettext
import os

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

from superlight import MARGIN, _, format_size
from superlight.common_disk import FolderScanner, delete_paths, move_to_trash


class FolderView(Gtk.Box):
    """Folder view, when browsing a storage chunk."""

    def __init__(self, window, leave):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0, border_width=MARGIN)
        self.window = window
        self.leave = leave          # called when going back from the first folder
        self.path = None
        self.scanner = None
        self.total = 0              # the size of the folder, as far as it is known
        self.changed = False        # True if something was moved to the trash or deleted
        self.show_hidden = True     # whether hidden files and folders are shown
        self.excluded = {}          # paths with their own row on the overview page, with their size. Left out here.

        header = Gtk.Box(spacing=MARGIN, margin_bottom=MARGIN)
        self.back_button = Gtk.Button.new_from_icon_name("xsi-go-previous-symbolic", Gtk.IconSize.BUTTON)
        self.back_button.set_tooltip_text(_("Back to the overview"))
        self.back_button.connect("clicked", lambda *_: self.go_back())
        header.pack_start(self.back_button, False, False, 0)
        self.crumbs = Gtk.Box()
        self.crumbs.get_style_context().add_class("linked")
        self.crumb_scroll = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER,
                                               hscrollbar_policy=Gtk.PolicyType.EXTERNAL)
        self.crumb_scroll.add(self.crumbs)
        header.pack_start(self.crumb_scroll, True, True, 0)
        self.spinner = Gtk.Spinner()
        header.pack_start(self.spinner, False, False, 0)
        self.refresh_button = Gtk.Button.new_from_icon_name("xsi-view-refresh-symbolic", Gtk.IconSize.BUTTON)
        self.refresh_button.set_tooltip_text(_("Measure again"))
        self.refresh_button.connect("clicked", lambda *_: self.open(self.path))
        header.pack_start(self.refresh_button, False, False, 0)
        self.pack_start(header, False, False, 0)

        # path, name, is a folder, size, modification time, icon, can be read
        self.store = Gtk.ListStore(str, str, bool, GObject.TYPE_INT64, GObject.TYPE_INT64, Gio.Icon, bool)
        self.sorted_store = Gtk.TreeModelSort(model=self.store)
        view = Gtk.TreeView(model=self.sorted_store, headers_visible=True)
        view.set_enable_search(False)
        view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        view.get_selection().connect("changed", self.on_selection_changed)
        view.connect("row-activated", self.on_row_activated)
        view.connect("key-press-event", self.on_key_pressed)
        view.connect("button-press-event", self.on_view_pressed)
        # Keep the list scrolled to the top until the user scrolls or clicks, because new rows are added above
        self.pinned = True
        for signal in ("scroll-event", "button-press-event", "key-press-event"):
            view.connect(signal, self.on_user_input)
        self.view = view
        name_column = Gtk.TreeViewColumn(_("Name"))
        icon_renderer = Gtk.CellRendererPixbuf()
        icon_renderer.set_property("stock-size", Gtk.IconSize.MENU)
        name_renderer = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.MIDDLE)
        name_column.pack_start(icon_renderer, False)
        name_column.pack_start(name_renderer, True)
        name_column.add_attribute(icon_renderer, "gicon", 5)
        name_column.add_attribute(name_renderer, "text", 1)
        name_column.set_cell_data_func(name_renderer, self.style_name)
        name_column.set_sort_column_id(1)
        name_column.set_expand(True)
        name_column.set_resizable(True)
        view.append_column(name_column)
        share_renderer = Gtk.CellRendererProgress()
        share_column = Gtk.TreeViewColumn(_("Share"), share_renderer)
        share_column.set_cell_data_func(share_renderer, self.format_share)
        share_column.set_min_width(130)
        view.append_column(share_column)
        size_renderer = Gtk.CellRendererText(xalign=1.0)
        size_column = Gtk.TreeViewColumn(_("Size"), size_renderer)
        size_column.set_cell_data_func(size_renderer, self.format_size)
        size_column.set_sort_column_id(3)
        size_column.set_min_width(90)
        view.append_column(size_column)
        modified_renderer = Gtk.CellRendererText(xalign=1.0)
        modified_column = Gtk.TreeViewColumn(_("Modified"), modified_renderer)
        modified_column.set_cell_data_func(modified_renderer, self.format_modified)
        modified_column.set_sort_column_id(4)
        modified_column.set_min_width(100)
        view.append_column(modified_column)
        # Biggest first
        self.sorted_store.set_sort_column_id(size_column.get_sort_column_id(), Gtk.SortType.DESCENDING)
        scroll = Gtk.ScrolledWindow()
        scroll.set_overlay_scrolling(False)
        scroll.set_shadow_type(Gtk.ShadowType.IN)
        scroll.add(view)
        self.pack_start(scroll, True, True, 0)

        self.note = Gtk.Label(wrap=True, margin=12)
        self.note.get_style_context().add_class("dim-label")
        self.note.set_no_show_all(True)
        self.pack_start(self.note, False, False, 0)

        bar = Gtk.Box(spacing=MARGIN, margin_top=MARGIN)
        self.summary = Gtk.Label(xalign=0)
        self.summary.get_style_context().add_class("dim-label")
        bar.pack_start(self.summary, True, True, 0)
        self.show_button = Gtk.Button(label=_("Show in File Manager"), sensitive=False)
        self.show_button.connect("clicked", self.on_show_clicked)
        bar.pack_end(self.show_button, False, False, 0)
        self.trash_button = Gtk.Button(label=_("Move to Trash"), sensitive=False)
        self.trash_button.connect("clicked", lambda *_: self.trash())
        bar.pack_end(self.trash_button, False, False, 0)
        self.pack_start(bar, False, False, 0)

    def start(self, path, show_hidden=True, excluded=None):
        """Opens a folder for the first time, with or without hidden files. The paths in excluded, as {path: size}, are
        not shown and are not counted in the size of the folder that contains them."""
        self.show_hidden = show_hidden
        self.excluded = {os.path.realpath(excluded_path): size for excluded_path, size in (excluded or {}).items()}
        self.changed = False
        self.open(path)

    def open(self, path):
        """Shows the content of a folder."""
        self.path = path.rstrip("/") or "/"
        self.pinned = True
        if self.scanner is not None:
            self.scanner.stop()
        self.store.clear()
        self.total = 0
        self.note.set_visible(False)
        self.spinner.start()
        self.spinner.set_visible(True)
        self.build_crumbs()
        self.on_selection_changed(self.view.get_selection())
        self.scanner = FolderScanner(self.path, self.on_entries, self.on_done)

    def go_back(self):
        """Goes back to the overview page."""
        self.stop()
        self.leave(self.changed)

    def build_crumbs(self):
        for crumb in self.crumbs.get_children():
            self.crumbs.remove(crumb)
        home = GLib.get_home_dir().rstrip("/")
        parts = []   # the title and the path of each button
        if self.path == home or self.path.startswith(home + "/"):
            parts.append((_("Home"), home))
            built = home
            names = [part for part in self.path[len(home):].split("/") if part]
        else:
            parts.append((_("File System"), "/"))
            built = ""
            names = [part for part in self.path.split("/") if part]
        for name in names:
            built = built + "/" + name
            parts.append((name, built))
        for title, path in parts:
            button = Gtk.Button(label=title, relief=Gtk.ReliefStyle.NORMAL)
            button.connect("clicked", self.on_crumb_clicked, path)
            self.crumbs.pack_start(button, False, False, 0)
        self.crumbs.show_all()
        # Make the last button visible
        GLib.idle_add(self.scroll_crumbs_to_end)

    def on_crumb_clicked(self, _button, path):
        self.open(path)

    def scroll_crumbs_to_end(self):
        adjustment = self.crumb_scroll.get_hadjustment()
        adjustment.set_value(adjustment.get_upper())
        return GLib.SOURCE_REMOVE

    def stop(self):
        if self.scanner is not None:
            self.scanner.stop()

    def on_entries(self, entries):
        for path, name, is_folder, size, modified, readable in entries:
            hidden = name.startswith(".")
            if hidden and not self.show_hidden:
                continue
            if self.excluded:
                real = os.path.realpath(path)
                if any(real == other or real.startswith(other + "/") for other in self.excluded):
                    continue
                # Subtract the size of the excluded paths that are inside this folder
                held = sum(held or 0 for other, held in self.excluded.items() if other.startswith(real + "/"))
                if held and size > 0:
                    size = max(0, size - held)
                    if size < 1024 * 1024:
                        continue   # what remains is only excluded paths
            if is_folder:
                icon = Gio.ThemedIcon.new("folder")
            else:
                key = os.path.splitext(name)[1].lower() or name
                if key not in self.icons:
                    self.icons[key] = Gio.content_type_get_icon(Gio.content_type_guess(name, None)[0])
                icon = self.icons[key]
            self.store.append((path, name, is_folder, size, modified, icon, readable))
            self.total += max(0, size)
        self.update_summary()
        self.keep_on_top()
        return GLib.SOURCE_REMOVE

    def on_user_input(self, *_):
        self.pinned = False
        return False

    def keep_on_top(self):
        if self.pinned:
            GLib.idle_add(self.scroll_to_top)

    def scroll_to_top(self):
        self.view.get_vadjustment().set_value(0)
        return GLib.SOURCE_REMOVE

    def on_done(self, total):
        self.spinner.stop()
        self.spinner.set_visible(False)
        # The bars show the share of the whole folder, so only use the total when nothing is filtered out
        if total is not None and self.show_hidden and not self.excluded:
            self.total = max(self.total, total)
        self.view.queue_draw()
        self.keep_on_top()
        if len(self.store) == 0:
            self.note.set_text(_("There is nothing in this folder."))
            self.note.set_visible(True)
        elif any(not row[6] for row in self.store):
            self.note.set_text(_("Folders you can't read are left out of the sizes."))
            self.note.set_visible(True)
        self.update_summary()
        return GLib.SOURCE_REMOVE

    icons = {}

    def style_name(self, _column, renderer, model, iterator, _data=None):
        # A folder that can't be read is gray and italic, because its size is not known
        readable = model.get_value(iterator, 6)
        renderer.set_property("style", Pango.Style.NORMAL if readable else Pango.Style.ITALIC)
        renderer.set_property("sensitive", readable)

    def format_share(self, _column, renderer, model, iterator, _data=None):
        size = model.get_value(iterator, 3)
        renderer.set_property("value", int(100 * size / self.total) if size > 0 and self.total else 0)
        renderer.set_property("text", "")

    def format_size(self, _column, renderer, model, iterator, _data=None):
        size = model.get_value(iterator, 3)
        renderer.set_property("text", format_size(size) if size >= 0 else _("Can't be read"))
        renderer.set_property("sensitive", size >= 0)

    def format_modified(self, _column, renderer, model, iterator, _data=None):
        modified = model.get_value(iterator, 4)
        renderer.set_property("text", GLib.DateTime.new_from_unix_local(modified).format("%x") if modified else "")

    def on_row_activated(self, view, path, _column):
        row = self.sorted_store[path]
        if row[2] and row[6]:
            self.open(row[0])

    def on_key_pressed(self, _view, event):
        # Backspace goes to the folder that holds this one
        if event.keyval == Gdk.KEY_BackSpace and self.path not in (None, "/"):
            self.open(os.path.dirname(self.path))
            return True
        # Delete moves what is selected to the trash, and Shift+Delete deletes it for good
        if event.keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete) and self.trash_button.get_sensitive():
            if event.state & Gdk.ModifierType.SHIFT_MASK:
                self.confirm_delete()
            else:
                self.trash()
            return True
        return False

    def on_view_pressed(self, view, event):
        """Right click: shows the menu for the selected rows, or for the row under the pointer if it isn't selected."""
        if event.button != 3 or event.window is not view.get_bin_window():
            return False
        found = view.get_path_at_pos(int(event.x), int(event.y))
        if found is None:
            return False
        if not view.get_selection().path_is_selected(found[0]):
            view.set_cursor(found[0])
        menu = Gtk.Menu()
        for label, key, modifiers, callback in (
                (_("Move to Trash"), Gdk.KEY_Delete, 0, self.trash),
                (_("Delete"), Gdk.KEY_Delete, Gdk.ModifierType.SHIFT_MASK, self.confirm_delete)):
            item = Gtk.MenuItem()
            accel_label = Gtk.AccelLabel(label=label, xalign=0)
            accel_label.set_accel(key, modifiers)
            item.add(accel_label)
            item.connect("activate", self.on_menu_item_activated, callback)
            menu.append(item)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def on_show_clicked(self, _button):
        """Shows the selected items in the file manager. If that is not possible, opens the folder."""
        paths = self.selected_paths()
        uris = [Gio.File.new_for_path(path).get_uri() for path in paths]
        try:
            Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, None, "org.freedesktop.FileManager1",
                "/org/freedesktop/FileManager1", "org.freedesktop.FileManager1", None).call_sync(
                    "ShowItems", GLib.Variant("(ass)", (uris, "")), Gio.DBusCallFlags.NONE, 5000, None)
        except GLib.Error:
            Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(os.path.dirname(paths[0])).get_uri(), None)

    def on_menu_item_activated(self, _item, callback):
        callback()

    def selected_paths(self):
        model, paths = self.view.get_selection().get_selected_rows()
        return [model[path][0] for path in paths]

    def on_selection_changed(self, selection):
        paths = selection.get_selected_rows()[1]
        self.show_button.set_sensitive(len(paths) == 1)
        self.trash_button.set_sensitive(len(paths) > 0)
        self.update_summary()

    def update_summary(self):
        model, paths = self.view.get_selection().get_selected_rows()
        if paths:
            size = sum(max(0, model[path][3]) for path in paths)
            self.summary.set_text(gettext.ngettext("%(count)d selected, %(size)s", "%(count)d selected, %(size)s", len(paths)) % {
                "count": len(paths), "size": format_size(size)})
        else:
            self.summary.set_text(gettext.ngettext("%(count)d item, %(size)s", "%(count)d items, %(size)s", len(self.store)) % {
                "count": len(self.store), "size": format_size(self.total)})

    def trash(self):
        """Asks for confirmation, then moves the selected items to the trash."""
        paths = self.selected_paths()
        model, rows = self.view.get_selection().get_selected_rows()
        size = sum(max(0, model[row][3]) for row in rows)
        if len(paths) == 1:
            question = _('Move "%s" to the trash?') % os.path.basename(paths[0])
        else:
            question = gettext.ngettext("Move %d item to the trash?", "Move %d items to the trash?", len(paths)) % len(paths)
        dialog = Gtk.MessageDialog(transient_for=self.window, modal=True, message_type=Gtk.MessageType.QUESTION,
                                   buttons=Gtk.ButtonsType.NONE, text=question)
        dialog.format_secondary_text(_("%s will be freed when the trash is emptied.") % format_size(size))
        dialog.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
        dialog.add_button(_("Move to Trash"), Gtk.ResponseType.ACCEPT)
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)
        dialog.get_widget_for_response(Gtk.ResponseType.ACCEPT).grab_focus()
        dialog.connect("response", self.on_trash_response, paths)
        dialog.present()

    def on_trash_response(self, dialog, response, paths):
        dialog.destroy()
        if response == Gtk.ResponseType.ACCEPT:
            self.remove(paths, move_to_trash, _("Could not move it to the trash"))

    def confirm_delete(self):
        paths = self.selected_paths()
        model, rows = self.view.get_selection().get_selected_rows()
        size = sum(max(0, model[row][3]) for row in rows)
        if len(paths) == 1:
            question = _('Permanently delete "%s"?') % os.path.basename(paths[0])
        else:
            question = gettext.ngettext("Permanently delete %d item?", "Permanently delete %d items?", len(paths)) % len(paths)
        dialog = Gtk.MessageDialog(transient_for=self.window, modal=True, message_type=Gtk.MessageType.QUESTION,
                                   buttons=Gtk.ButtonsType.NONE, text=question)
        dialog.format_secondary_text(_("%s will be freed. This can't be undone.") % format_size(size))
        dialog.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
        dialog.add_button(_("Delete"), Gtk.ResponseType.ACCEPT)
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)
        dialog.get_widget_for_response(Gtk.ResponseType.ACCEPT).grab_focus()
        dialog.connect("response", self.on_delete_response, paths)
        dialog.present()

    def on_delete_response(self, dialog, response, paths):
        dialog.destroy()
        if response == Gtk.ResponseType.ACCEPT:
            self.remove(paths, delete_paths, _("Could not delete it"))

    def remove(self, paths, action, error_title):
        """Moves paths to the trash or deletes them, and removes them from the list."""
        failures = action(paths)
        failed = {path for path, _message in failures}
        for row in [row for row in self.store if row[0] in paths and row[0] not in failed]:
            self.total = max(0, self.total - max(0, row[3]))
            self.store.remove(row.iter)
            self.changed = True
        self.update_summary()
        if failures:
            error = Gtk.MessageDialog(transient_for=self.window, modal=True, message_type=Gtk.MessageType.ERROR,
                                      buttons=Gtk.ButtonsType.CLOSE, text=error_title)
            error.format_secondary_text("\n".join("%s: %s" % (os.path.basename(path), message) for path, message in failures))
            error.connect("response", lambda dialog, _response: dialog.destroy())
            error.present()
