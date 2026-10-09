from gi.repository import GLib

from superlight import _, format_size
from superlight.chunks.chunk import MINIMUM_SHOWN, Chunk, CleanupAction
from superlight.common_disk import empty_directory


class Cache(Chunk):
    key = "cache"
    name = _("Cache")
    icon = "xsi-edit-clear-all-symbolic"
    color = "#D95930"

    def detect(self, taken):
        return [GLib.get_user_cache_dir()]

    def get_cleanup_action(self, size, paths):
        if size < MINIMUM_SHOWN:
            return None
        return CleanupAction(_("Clear Cache…"), _("Clear the cache?"),
                       _("%s of temporary files will be deleted. Programs can be slower the first time they start "
                         "afterwards.") % format_size(size),
                       _("Clear Cache"), lambda: empty_directory(GLib.get_user_cache_dir()))
