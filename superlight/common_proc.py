import os
import pwd
from functools import lru_cache


from superlight import _


@lru_cache(maxsize=None)
def gpu_name(path):
    """Returns the name of the GPU in the given /sys device directory, or "Unknown GPU"."""
    try:
        identifiers = {}
        for field in ("vendor", "device", "revision"):
            with open(f"{path}/{field}") as source:
                identifiers[field] = int(source.read(), 16)
        if identifiers["vendor"] == 0x1002:
            with open("/usr/share/libdrm/amdgpu.ids") as source:
                for line in source:
                    fields = line.strip().split(",", 2)
                    if len(fields) != 3 or line.startswith("#"):
                        continue
                    if (int(fields[0], 16), int(fields[1], 16)) == (
                            identifiers["device"], identifiers["revision"]):
                        return fields[2].strip()
    except (OSError, ValueError):
        pass
    return _("Unknown GPU")


def read_memory_usage():
    """Returns the bytes of memory used, memory total, swap used and swap total."""
    with open("/proc/meminfo", encoding="utf-8") as source:
        info = {key: int(value.split()[0]) * 1024
                for key, value in (line.split(":", 1) for line in source)}
    memory_total = info["MemTotal"]
    memory_used = max(0, memory_total - info["MemAvailable"])
    swap_total = info["SwapTotal"]
    swap_used = max(0, swap_total - info["SwapFree"])
    return memory_used, memory_total, swap_used, swap_total


def read_cpu_times():
    """Returns the idle time and the total time of all the processors together, in clock ticks."""
    with open("/proc/stat", encoding="utf-8") as stat:
        fields = next(line.split() for line in stat if line.startswith("cpu "))
    values = [int(value) for value in fields[1:9]]
    return values[3] + values[4], sum(values)


def read_core_times():
    """Returns the idle time and the total time of each processor core, in clock ticks, by name (cpu0, cpu1..)."""
    cores = {}
    with open("/proc/stat", encoding="utf-8") as source:
        for line in source:
            fields = line.split()
            if fields and fields[0].startswith("cpu") and fields[0][3:].isdigit():
                values = [int(value) for value in fields[1:9]]
                cores[fields[0]] = (values[3] + values[4], sum(values))
    return cores


def read_network_bytes():
    """Returns the bytes received and sent since the system started, for all the network interfaces except lo."""
    received = sent = 0
    with open("/proc/net/dev", encoding="utf-8") as source:
        for line in list(source)[2:]:
            name, _separator, data = line.partition(":")
            if name.strip() == "lo":
                continue
            fields = data.split()
            received += int(fields[0])
            sent += int(fields[8])
    return received, sent


# Virtual devices and devices that sit on top of other devices. Counting them would count the same bytes twice.
IGNORED_DISKS = ("loop", "ram", "zram", "dm-", "md", "sr", "fd", "nbd")


def read_disk_bytes():
    """Returns the bytes read and written since the system started, for whole disks (not their partitions)."""
    read = written = 0
    with open("/proc/diskstats", encoding="utf-8") as source:
        for line in source:
            fields = line.split()
            if fields[2].startswith(IGNORED_DISKS) or not os.path.isdir(f"/sys/block/{fields[2]}"):
                continue
            read += int(fields[5]) * 512
            written += int(fields[9]) * 512
    return read, written


# The sensor chips to read for each kind, with the labels of the preferred sensor when a chip has several
TEMPERATURE_CHIPS = {"cpu": {"coretemp": ("Package id 0",), "k10temp": ("Tdie", "Tctl"), "zenpower": ("Tdie",)},
                     "gpu": {"amdgpu": ("edge",), "nouveau": (), "radeon": ()},
                     "disk": {"nvme": ("Composite",), "drivetemp": ()}}


def read_text(path):
    """Returns the content of a small file, without the white space around it."""
    with open(path, encoding="utf-8") as source:
        return source.read().strip()


def read_temperatures(sensors):
    """Returns the highest temperature for each kind (cpu, gpu, disk) in degrees Celsius, or None if there is none."""
    temperatures = []
    for kind in TEMPERATURE_CHIPS:
        values = []
        for path in sensors[kind]:
            try:
                values.append(int(read_text(path)) / 1000)
            except (OSError, ValueError):
                continue
        temperatures.append(max(values) if values else None)
    return tuple(temperatures)


def read_fans(fans):
    """Returns the speed of each fan in revolutions per minute, or None for a fan that can't be read."""
    speeds = []
    for _name, path in fans:
        try:
            speeds.append(int(read_text(path)))
        except (OSError, ValueError):
            speeds.append(None)
    return tuple(speeds)


def process_stat(pid):
    """Returns some values from /proc/<pid>/stat for a process."""
    with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as source:
        line = source.read()
    _, rest = line.split("(", 1)
    name, rest = rest.rsplit(")", 1)
    fields = rest.split()
    # name, CPU ticks, start time, resident pages, state, nice level, virtual bytes, parent PID
    return (name, int(fields[11]) + int(fields[12]), fields[19], max(0, int(fields[21])), fields[0],
            int(fields[16]), int(fields[20]), int(fields[1]))


@lru_cache(maxsize=None)
def boot_time():
    """Returns when the system started, in seconds since 1970."""
    with open("/proc/stat", encoding="utf-8") as source:
        return next(int(line.split()[1]) for line in source if line.startswith("btime "))


@lru_cache(maxsize=256)
def username(uid):
    """Returns the name of a user from the user ID, or the ID as text if it has no name."""
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)
