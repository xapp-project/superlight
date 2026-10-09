import os

from gi.repository import Gio, GLib

from superlight import _
from superlight.chunks.chunk import Chunk
from superlight.common_disk import read_mounts


class Snapshots(Chunk):
    """The system snapshots made by Timeshift. Clicking opens Timeshift, if it is installed."""

    key = "snapshots"
    name = _("System Snapshots")
    icon = "xsi-document-open-recent-symbolic"
    color = "#739ECF"

    def detect(self, taken):
        # Timeshift keeps its snapshots in a folder at the top of each disk
        found = []
        for mount in read_mounts():
            real = os.path.realpath(os.path.join(mount.path, "timeshift"))
            if os.path.isdir(real) and real not in found:
                found.append(real)
        return found

    def click(self, page):
        # Timeshift deletes snapshots itself, and asks for the privileges it needs. Without it, browse the folder.
        app = None
        for name in ("timeshift-gtk.desktop", "timeshift.desktop"):
            app = Gio.DesktopAppInfo.new(name)
            if app is not None:
                break
        if app is None:
            page.browse(self)
            return
        try:
            app.launch([], None)
        except GLib.Error:
            pass
