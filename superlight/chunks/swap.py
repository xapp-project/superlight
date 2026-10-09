import os


from superlight import _
from superlight.chunks.chunk import MINIMUM_SYSTEM, Chunk
from superlight.common_disk import decode_mount_path


class Swap(Chunk):
    key = "swap"
    name = _("Swap")
    icon = "xsi-media-flash-symbolic"
    color = "#B26666"
    minimum = MINIMUM_SYSTEM

    def detect(self, taken):
        # Only swap files. Swap partitions are on their own drive/volume.
        files = []
        try:
            with open("/proc/swaps", encoding="utf-8") as source:
                next(source, None)
                for line in source:
                    fields = line.split()
                    if len(fields) > 1 and fields[1] == "file":
                        files.append(os.path.realpath(decode_mount_path(fields[0])))
        except OSError:
            pass
        return [path for path in files if os.path.isfile(path)]

    def click(self, page):
        pass
