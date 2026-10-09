import os
import re
import shutil
import stat
import subprocess
import threading
from collections import namedtuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib

from superlight import _


# Storage filesystems
STORAGE_FILESYSTEMS = {"ext2", "ext3", "ext4", "btrfs", "xfs", "f2fs", "jfs", "reiserfs", "vfat", "exfat", "ntfs",
                    "ntfs3", "fuseblk", "zfs", "udf", "hfsplus", "bcachefs"}
CONCURRENT_MEASURES = 3

Mount = namedtuple("Mount", ("path", "device", "filesystem"))
Drive = namedtuple("Drive", ("mount", "name", "device", "total", "used", "free"))


def decode_mount_path(path):
    """Turns a path from /proc/mounts back into the real path. That file writes a space as \\040 (a backslash and
    the character's number in base 8) and does the same for tabs, newlines and backslashes. "/media/My\\040Disk"
    becomes "/media/My Disk"."""
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), path)


def read_mounts():
    """Returns the file systems of block devices that are mounted, as /proc/self/mountinfo lists them."""
    mounts = []
    with open("/proc/self/mountinfo", encoding="utf-8") as source:
        for line in source:
            before, _separator, after = line.partition(" - ")
            fields = before.split()
            filesystem, device = after.split()[:2]
            path = decode_mount_path(fields[4])
            if filesystem in STORAGE_FILESYSTEMS and device.startswith("/dev/") and not device.startswith("/dev/loop"):
                mounts.append(Mount(path, device, filesystem))
    return mounts


def list_drives():
    """Returns the disks that are mounted, starting with the one used for /.
    A disk that is mounted several times (the subvolumes of btrfs) counts as one, i.e. the shortest of its mount points."""
    names = {}
    for mount in Gio.VolumeMonitor.get().get_mounts():
        root = mount.get_root().get_path()
        if root:
            names[root] = mount.get_name()
    by_device = {}
    for mount in read_mounts():
        if mount.path == "/boot" or mount.path.startswith("/boot/"):
            continue
        known = by_device.get(mount.device)
        if known is None or len(mount.path) < len(known.path):
            by_device[mount.device] = mount
    drives = []
    for mount in by_device.values():
        try:
            stat = os.statvfs(mount.path)
        except OSError:
            continue
        total = stat.f_blocks * stat.f_frsize
        if total > 0:
            name = _("System drive") if mount.path == "/" else names.get(mount.path) or os.path.basename(mount.path)
            drives.append(Drive(mount.path, name, mount.device, total, (stat.f_blocks - stat.f_bfree) * stat.f_frsize,
                                stat.f_bavail * stat.f_frsize))
    return sorted(drives, key=lambda drive: (drive.mount != "/", drive.mount))


def mount_of(path, mounts):
    """Returns the mount that a path is on. The one with the longest mount point that starts it."""
    path = os.path.realpath(path)
    best = None
    for mount in mounts:
        if path == mount.path or path.startswith(mount.path.rstrip("/") + "/"):
            if best is None or len(mount.path) > len(best.path):
                best = mount
    return best


def existing(paths):
    """Filters and returns only the paths that are actually there. A link counts for what it leads to."""
    found = []
    for path in paths:
        real = os.path.realpath(path)
        if real not in found and os.path.exists(real):
            found.append(real)
    return found


class Measurer:
    """Measures how much space folders take, by running the du command on each one.

    du counts the space a file really uses on the disk, counts a file with several names (hard links) only
    once, and does not go into other file systems.

    Several folders are measured at the same time, each by its own thread. As soon as a folder is done, the
    callback is called with the path and its size (None if it could not be measured), in the main loop."""

    def __init__(self, callback):
        self.callback = callback
        self.processes = set()
        self.lock = threading.Lock()
        self.slots = threading.Semaphore(CONCURRENT_MEASURES)
        self.stopped = False

    def measure(self, paths):
        for path in paths:
            threading.Thread(target=self.work, args=(path,), daemon=True).start()

    def work(self, path):
        size = None
        with self.slots:
            if self.stopped:
                return
            try:
                process = subprocess.Popen(["du", "-sx", "-B1", "--", path], stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, text=True)
                with self.lock:
                    self.processes.add(process)
                output, _errors = process.communicate()
                with self.lock:
                    self.processes.discard(process)
                if output.strip():
                    size = int(output.split("\t", 1)[0])
            except (OSError, ValueError):
                size = None
        if not self.stopped:
            GLib.idle_add(self.callback, path, size)

    def stop(self):
        self.stopped = True
        with self.lock:
            for process in list(self.processes):
                process.terminate()


def empty_directory(path):
    """Delete what is in a folder, and keep the folder. A link is deleted, not what it leads to. Returns the
    number of things that could not be deleted."""
    failures = 0
    try:
        entries = list(os.scandir(path))
    except OSError:
        return 1
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
            else:
                os.unlink(entry.path)
        except OSError:
            failures += 1
    return failures


class FolderScanner:
    """Lists what is directly inside a folder, with the space each item takes, by running the du command.

    It starts as soon as it is created, in its own thread, and reports as it goes. entries_callback is called,
    in the main loop, with a list of the items found since the previous call. Each item is a tuple:
    (path, name, is_folder, size, modified, readable). modified is in seconds since 1970. The size of a folder
    that can't be opened is -1, and folders with nothing in them are left out.

    When du is finished, done_callback is called with the size of the whole folder (None if it could not be
    measured)."""

    def __init__(self, path, entries_callback, done_callback):
        self.path = path.rstrip("/") or "/"
        self.entries_callback = entries_callback
        self.done_callback = done_callback
        self.process = None
        self.stopped = False
        threading.Thread(target=self.work, daemon=True).start()

    def work(self):
        total = None
        batch = []
        try:
            self.process = subprocess.Popen(
                ["du", "-a", "-x", "-0", "--max-depth=1", "-B1", "--time", "--time-style=+%s", "--", self.path],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            pending = b""
            while not self.stopped:
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    break
                pending += chunk
                *records, pending = pending.split(b"\0")
                for record in records:
                    entry = self.parse(record)
                    if entry is None:
                        continue
                    if entry[0] == self.path:
                        total = entry[3]
                    else:
                        batch.append(entry)
                if batch and not self.stopped:
                    GLib.idle_add(self.entries_callback, batch)
                    batch = []
            self.process.wait()
        except OSError:
            pass
        if not self.stopped:
            if batch:
                GLib.idle_add(self.entries_callback, batch)
            GLib.idle_add(self.done_callback, total)

    def parse(self, record):
        try:
            size, modified, path = record.decode("utf-8", errors="surrogateescape").split("\t", 2)
            path = os.fsdecode(os.fsencode(path))
            mode = os.lstat(path).st_mode
        except (ValueError, OSError):
            return None
        is_folder = stat.S_ISDIR(mode)
        readable = not is_folder or os.access(path, os.R_OK | os.X_OK)
        if is_folder and readable and path != self.path:
            # A folder with nothing in it is not listed
            try:
                with os.scandir(path) as entries:
                    if next(entries, None) is None:
                        return None
            except OSError:
                pass
        return (path, os.path.basename(path), is_folder, int(size) if readable else -1, int(modified), readable)

    def stop(self):
        self.stopped = True
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()


def delete_paths(paths):
    """Deletes files and folders for good. Links get deleted, not what they lead to. Returns the (path, message) of
    the paths which couldn't be deleted."""
    failures = []
    for path in paths:
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)
        except OSError as error:
            failures.append((path, error.strerror or str(error)))
    return failures


def move_to_trash(paths):
    """Moves files and folders to the trash. Returns the (path, message) of the paths which couldn't be moved."""
    failures = []
    for path in paths:
        try:
            Gio.File.new_for_path(path).trash(None)
        except GLib.Error as error:
            failures.append((path, error.message))
    return failures
