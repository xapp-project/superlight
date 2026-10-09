from abc import ABC, abstractmethod

from superlight import format_size

MINIMUM_SYSTEM = 1_000_000_000   # the system's cache, logs, swap and temp files only get a row above this size
MINIMUM_SHOWN = 50_000_000   # anything smaller gets no row and no button, it is counted in the rest


class CleanupAction:
    """Contains the information to make a cleanup button and connect it to an action (a callback)."""

    def __init__(self, label, question, warning, button, action):
        self.label = label
        self.question = question
        self.warning = warning
        self.button = button
        self.action = action


class Chunk(ABC):
    """A type of used space in the storage page (Trash, Cache, Flatpak..). This is the generic class. Each chunk type has a python file with a class which derives this one."""

    key = name = icon = color = None
    minimum = MINIMUM_SHOWN         # below this minimum size, the chunk is not shown
    always_listed = False           # dictates whether or not the chunk always shows up in the page, even when empty.
    root = None                     # Where to start browsing. If None, the biggest of the chunk's paths on the drive.

    def __init__(self):
        self.paths = []

    @abstractmethod
    def detect(self, taken):
        """Returns the list of paths of this chunk. taken has the paths that the previous chunks already took."""

    def click(self, page):
        """Called when the chunk is clicked."""
        page.browse(self)

    def get_display_size(self, size, paths):
        """Returns the text showns on the storage page, or None if the chunk should not be listed."""
        return format_size(size) if size >= self.minimum else None

    def get_cleanup_action(self, size, paths):
        """Returns a CleanupAction for the page, or None."""
        return None
