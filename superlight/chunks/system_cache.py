
from superlight import _
from superlight.chunks.chunk import MINIMUM_SYSTEM, Chunk


class SystemCache(Chunk):
    key = "syscache"
    name = _("System cache")
    icon = "xsi-emblem-system-symbolic"
    color = "#99804C"
    minimum = MINIMUM_SYSTEM

    def detect(self, taken):
        return ["/var/cache"]
