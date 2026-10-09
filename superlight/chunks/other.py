from superlight import _
from superlight.chunks.chunk import Chunk


class Other(Chunk):
    """The used space that no other chunk accounts for: the system, files outside the home folder, files that can't
    be read. It is calculated, not detected."""

    key = "other"
    name = _("Other")
    icon = "xsi-drive-harddisk-system-symbolic"
    color = "#5E5E59"

    def detect(self, taken):
        return []

    def click(self, page):
        pass
