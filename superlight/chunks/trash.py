import os

from gi.repository import Gio, GLib

from superlight import _, format_size
from superlight.chunks.chunk import Chunk, CleanupAction
from superlight.common_disk import empty_directory, read_mounts


class Trash(Chunk):
    """The trash of the user. It is always listed, so that it can be opened, and it says "Empty" when it is."""

    key = "trash"
    name = _("Trash")
    icon = "xsi-user-trash-symbolic"
    color = "#D4547D"
    always_listed = True

    def detect(self, taken):
        # The trash in the home folder, and the one at the top of each disk (.Trash-<uid> or .Trash/<uid>). The file
        # manager shows them as one trash.
        folders = [os.path.join(GLib.get_user_data_dir(), "Trash")]
        uid = os.getuid()
        for mount in read_mounts():
            folders += [os.path.join(mount.path, ".Trash-%d" % uid), os.path.join(mount.path, ".Trash", str(uid))]
        found = []
        for path in folders:
            real = os.path.realpath(path)
            if os.path.isdir(real) and real not in found:
                found.append(real)
        return found

    @staticmethod
    def holds_files(trash_folders):
        """Returns True if there is a file in one of the trash folders. The leftovers of an emptied trash (its empty
        folders, a file of sizes) don't count."""
        for path in trash_folders:
            try:
                if os.listdir(os.path.join(path, "files")):
                    return True
            except OSError:
                pass
        return False

    def get_display_size(self, size, paths):
        return format_size(size) if self.holds_files(paths) else _("Empty")

    def get_cleanup_action(self, size, paths):
        if not self.holds_files(paths):
            return None

        def empty():
            """Empties the trash folders. Returns the number of failures."""
            failures = 0
            for trash in paths:
                for name in ("files", "info"):
                    if os.path.isdir(os.path.join(trash, name)):
                        failures += empty_directory(os.path.join(trash, name))
                try:
                    os.unlink(os.path.join(trash, "directorysizes"))
                except OSError:
                    pass
            return failures

        return CleanupAction(_("Empty Trash…"), _("Empty the trash?"), _("%s will be deleted for good.") % format_size(size),
                       _("Empty Trash"), empty)

    def click(self, page):
        # Open the trash in the file manager, which shows the files with their original names
        try:
            Gio.AppInfo.launch_default_for_uri("trash:///", None)
        except GLib.Error:
            pass
