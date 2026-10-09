from gi.repository import GLib

from superlight import _
from superlight.chunks.chunk import Chunk


class Hidden(Chunk):
    """The files and folders of the home folder that start with a dot: the settings and data of programs. It has no
    paths of its own, it is whatever is left of the home folder."""

    key = "hidden"
    name = _("Hidden files")
    icon = "xsi-folder-symbolic"
    color = "#8C8C85"
    root = GLib.get_home_dir()

    def detect(self, taken):
        # This is the last chunk. Remember the paths of the others, to leave them out when browsing
        self.excluded = list(taken)
        return []

    def click(self, page):
        page.browse(self, exclude_paths=self.excluded)
