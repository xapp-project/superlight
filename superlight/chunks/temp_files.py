
from superlight import _
from superlight.chunks.chunk import MINIMUM_SYSTEM, Chunk


class Temp(Chunk):
    key = "temp"
    name = _("Temp files")
    icon = "xsi-text-x-generic-symbolic"
    color = "#8080B2"
    minimum = MINIMUM_SYSTEM

    def detect(self, taken):
        return ["/tmp", "/var/tmp"]

    def click(self, page):
        pass
