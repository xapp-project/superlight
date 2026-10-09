
from superlight import _
from superlight.chunks.chunk import MINIMUM_SYSTEM, Chunk


class Logs(Chunk):
    key = "logs"
    name = _("Log files")
    icon = "xsi-view-list-symbolic"
    color = "#668C99"
    minimum = MINIMUM_SYSTEM

    def detect(self, taken):
        return ["/var/log"]
