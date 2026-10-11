import os
import re
from collections import namedtuple

from superlight import _

ROOT = "/sys/fs/cgroup"
UNIT_SUFFIXES = (".slice", ".service", ".scope", ".socket", ".mount", ".swap")

# A control group of the unified hierarchy. -1 means not known (file missing or unreadable). cpu is the percent
# of all the processors used since the previous scan, filled in by CgroupRates.
Cgroup = namedtuple("Cgroup", ("path", "parent", "name", "memory", "swap", "peak", "cpu_usec", "oom_kills",
                               "pressure", "pids", "cpu"), defaults=(-1,))
GAP = 5_000_000   # microseconds. After this long without a scan, rates start again from zero


def parse_counters(text):
    """Parses "key value" lines, as in cpu.stat and memory.events."""
    counters = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 2:
            try:
                counters[fields[0]] = int(fields[1])
            except ValueError:
                continue
    return counters


def parse_pressure(text):
    """Returns the "some avg10" value of a pressure file, or -1."""
    for line in text.splitlines():
        if line.startswith("some "):
            match = re.search(r"\bavg10=([\d.]+)", line)
            if match:
                return float(match.group(1))
    return -1


def unit_of(path):
    """The unit a cgroup path belongs to: its last .service or .scope, else its last part."""
    names = [part for part in path.split("/") if part]
    return next((part for part in reversed(names) if part.endswith((".service", ".scope"))),
                names[-1] if names else "")


def parse_proc_cgroup(text):
    """Returns the cgroup path and the unit of a process from the content of /proc/<pid>/cgroup."""
    for line in text.splitlines():
        fields = line.split(":", 2)
        if len(fields) == 3 and fields[1] in ("", "name=systemd"):
            return fields[2], unit_of(fields[2])
    return "", ""


def unit_object_path(name):
    """The D-Bus object path of a unit in systemd's manager: letters and digits stay, every other byte is _XX."""
    escaped = "".join(char if char.isascii() and char.isalnum() else "".join("_%02x" % byte for byte in char.encode())
                      for char in name)
    return "/org/freedesktop/systemd1/unit/" + escaped


def oom_handling(preference, pressure):
    """What systemd-oomd does with a unit, from its ManagedOOMPreference and ManagedOOMMemoryPressure."""
    parts = []
    if preference == "omit":
        parts.append(_("Exempt"))
    elif preference == "avoid":
        parts.append(_("Avoid"))
    if pressure == "kill":
        parts.append(_("Kill on pressure"))
    return ", ".join(parts)


def session_prefix(uid):
    return f"/user.slice/user-{uid}.slice"


def in_session(path, uid):
    prefix = session_prefix(uid)
    return path == prefix or path.startswith(prefix + "/")


def manager_for(path, uid):
    """Which systemd manager knows the unit of a cgroup: "session" for the user's own, "system" for the system's,
    None for other users' units and for directories that are not units."""
    if not path.rsplit("/", 1)[-1].endswith(UNIT_SUFFIXES):
        return None
    match = re.match(r"^/user\.slice/user-(\d+)\.slice/user@\d+\.service/", path)
    if match is None:
        return "system"
    return "session" if int(match.group(1)) == uid else None


def unit_metadata(unit_properties, type_properties):
    """The description and the OOM handling of a unit, from the properties of its Unit interface and of its
    type's interface (Scope, Service, Slice...)."""
    return (str(unit_properties.get("Description", "")),
            oom_handling(type_properties.get("ManagedOOMPreference"), type_properties.get("ManagedOOMMemoryPressure")))


def assemble_tree(cgroups, processes, uid, session_only):
    """Picks the rows of the tree: the cgroups (only the user's session with session_only) and the processes that
    run in one of them. Returns them with the parent of each row ({key: parent key or None}, keyed by cgroup path
    or process key) and the number of processes under each cgroup, children included."""
    if session_only:
        cgroups = [cgroup for cgroup in cgroups if in_session(cgroup.path, uid)]
    shown = {cgroup.path for cgroup in cgroups}
    parents = {cgroup.path: cgroup.parent if cgroup.parent in shown else None for cgroup in cgroups}
    processes = [process for process in processes if process.cgroup in shown]
    counts = dict.fromkeys(shown, 0)
    for process in processes:
        parents[process.key] = process.cgroup
        path = process.cgroup
        while path is not None:
            counts[path] += 1
            path = parents[path]
    return cgroups, processes, parents, counts


def read_int(directory, filename):
    try:
        with open(os.path.join(directory, filename), encoding="utf-8") as source:
            return int(source.read().strip())
    except (OSError, ValueError):
        return -1


def read_text(directory, filename):
    try:
        with open(os.path.join(directory, filename), encoding="utf-8") as source:
            return source.read()
    except OSError:
        return ""


def read_cgroups(root=ROOT):
    """Walks the cgroup filesystem and returns a Cgroup for every directory below the root."""
    cgroups = []
    pending = [(root, None)]
    while pending:
        directory, parent = pending.pop()
        try:
            entries = [entry for entry in os.scandir(directory) if entry.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        for entry in entries:
            path = (parent or "") + "/" + entry.name
            pids = tuple(int(pid) for pid in read_text(entry.path, "cgroup.procs").split())
            cgroups.append(Cgroup(path, parent, entry.name, read_int(entry.path, "memory.current"),
                                  read_int(entry.path, "memory.swap.current"), read_int(entry.path, "memory.peak"),
                                  parse_counters(read_text(entry.path, "cpu.stat")).get("usage_usec", -1),
                                  parse_counters(read_text(entry.path, "memory.events")).get("oom_kill", -1),
                                  parse_pressure(read_text(entry.path, "memory.pressure")), pids))
            pending.append((entry.path, path))
    return cgroups


class CgroupRates:
    """The CPU use of each cgroup since the previous scan, as a percent of all the processors together."""

    def __init__(self, processors=None):
        self.processors = processors or os.cpu_count() or 1
        self.previous = {}
        self.last_time = None

    def update(self, now, cgroups):
        """Returns {path: percent} for the cgroups, -1 for those whose CPU time is not known."""
        if self.last_time is None or now - self.last_time > GAP:
            self.previous = {}
            elapsed = None
        else:
            elapsed = max(0.000001, (now - self.last_time) / 1_000_000)
        rates, current = {}, {}
        for cgroup in cgroups:
            if cgroup.cpu_usec < 0:
                rates[cgroup.path] = -1
                continue
            current[cgroup.path] = cgroup.cpu_usec
            old = self.previous.get(cgroup.path)
            if elapsed is None or old is None:
                rates[cgroup.path] = 0.0
            else:
                rates[cgroup.path] = min(100.0, max(0, cgroup.cpu_usec - old) / 1_000_000 / elapsed * 100
                                         / self.processors)
        self.previous = current
        self.last_time = now
        return rates
