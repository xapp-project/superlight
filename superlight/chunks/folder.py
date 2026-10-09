from superlight.chunks.chunk import Chunk


class Folder(Chunk):
    """A folder or a file at the top of a drive that no other chunk has. One is created for each of them. The color
    is set when the rows are sorted."""

    def __init__(self, path, name, is_folder):
        super().__init__()
        self.key = path
        self.name = name
        self.icon = "xsi-folder-symbolic" if is_folder else "xsi-emblem-documents-symbolic"
        self.paths = [path] if is_folder else []
        self.is_folder = is_folder

    def detect(self, taken):
        return self.paths

    def click(self, page):
        if self.is_folder:
            page.browse(self)
