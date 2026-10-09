import os

from gi.repository import GLib

from superlight import _
from superlight.chunks.chunk import Chunk


class Personal(Chunk):
    """The files and folders of the home folder that don't start with a dot, except those taken by other chunks.
    Links are skipped, because what they lead to is counted where it is."""

    key = "personal"
    name = _("Personal files")
    icon = "xsi-folder-documents-symbolic"
    color = "#8078DE"
    root = GLib.get_home_dir()

    def click(self, page):
        page.browse(self, show_hidden=False)

    def detect(self, taken):
        taken = [os.path.realpath(path) for path in taken]
        try:
            entries = sorted(os.scandir(self.root), key=lambda entry: entry.name)
        except OSError:
            return []
        kept = []
        for entry in entries:
            if entry.name.startswith(".") or entry.is_symlink():
                continue
            path = entry.path
            # Skip paths taken by another chunk, and folders that contain such a path
            if any(path == other or path.startswith(other + "/") or other.startswith(path + "/") for other in taken):
                continue
            kept.append(path)
        return kept
