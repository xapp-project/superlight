"""Reads information from NVIDIA GPUs through NVML, the library that comes with the NVIDIA driver.

Unlike the other drivers, the NVIDIA driver doesn't provide the percentage of time the GPU is busy in /sys, it has
no hwmon sensors and it doesn't report the engines used by each client in /proc/<pid>/fdinfo. NVML provides all of
that instead.

Everything here returns None or empty results when NVML is not installed, when the driver is not loaded (for
example when the card uses nouveau) or when a card doesn't support a query. Call get() to read the GPUs once."""

import ctypes
import threading
import time


from superlight import _


SUCCESS = 0
ERROR_INSUFFICIENT_SIZE = 7

TEMPERATURE_GPU = 0
# The number of samples read from a GPU at once. NVML says how many it needs when this is too small.
SAMPLES = 128
# Samples older than this, in microseconds, belong to a process that is not using the GPU any more. NVML keeps a
# history of them, which would otherwise show work that stopped a while ago.
SAMPLE_AGE = 3_000_000
# NVML uses this value to say that it can't tell how much video memory a process uses
UNKNOWN_MEMORY = 0xFFFFFFFFFFFFFFFF
# The version of a structure added after the first version of NVML: its size in the low bits, the version above,
# as in the NVML_STRUCT_VERSION macro of nvml.h.
STRUCT_VERSION_1 = 1 << 24


class _Utilization(ctypes.Structure):
    """How much of the GPU and of its video memory was used during the last second, as nvmlUtilization_t."""
    _fields_ = (("gpu", ctypes.c_uint), ("memory", ctypes.c_uint))


class _Process(ctypes.Structure):
    """A process that uses a GPU, as nvmlProcessInfo_t."""
    _fields_ = (("pid", ctypes.c_uint), ("memory", ctypes.c_ulonglong),
                ("gpu_instance", ctypes.c_uint), ("compute_instance", ctypes.c_uint))


class _Sample(ctypes.Structure):
    """The usage of a GPU by a process during a second, as nvmlProcessUtilizationSample_t."""
    _fields_ = (("pid", ctypes.c_uint), ("timestamp", ctypes.c_ulonglong), ("gpu", ctypes.c_uint),
                ("memory", ctypes.c_uint), ("encoder", ctypes.c_uint), ("decoder", ctypes.c_uint))


class _FanSpeed(ctypes.Structure):
    """The speed of one fan, as nvmlFanSpeedInfo_t."""
    _fields_ = (("version", ctypes.c_uint), ("fan", ctypes.c_uint), ("speed", ctypes.c_uint))


class Device:
    """One NVIDIA GPU."""

    def __init__(self, library, handle, name):
        self.library = library
        self.handle = handle
        self.name = name
        # The number of fans is read once, so that a fan that can't be read keeps its place in the list
        self.fan_count = 0
        count = ctypes.c_uint()
        if self.call("nvmlDeviceGetNumFans", ctypes.byref(count)) == SUCCESS:
            self.fan_count = count.value
        else:
            # Older drivers don't have nvmlDeviceGetNumFans. Ask for the fans one by one instead.
            while self.fan_count < 8 and self.fan_speed(self.fan_count) is not None:
                self.fan_count += 1

    def call(self, name, *arguments):
        """Calls a function of NVML for this device. Returns None if the installed driver doesn't have it."""
        return _call(self.library, name, self.handle, *arguments)

    def usage(self):
        """Returns the percentage of time the GPU was busy during the last second, or None if it can't be read."""
        utilization = _Utilization()
        if self.call("nvmlDeviceGetUtilizationRates", ctypes.byref(utilization)) != SUCCESS:
            return None
        return max(0.0, min(100.0, float(utilization.gpu)))

    def temperature(self):
        """Returns the temperature of the GPU in degrees Celsius, or None if it can't be read."""
        value = ctypes.c_uint()
        if self.call("nvmlDeviceGetTemperature", TEMPERATURE_GPU, ctypes.byref(value)) != SUCCESS:
            return None
        return float(value.value)

    def fan_speed(self, index):
        """Returns the speed of a fan in revolutions per minute, or None if it can't be read."""
        fan = _FanSpeed()
        fan.version = ctypes.sizeof(_FanSpeed) | STRUCT_VERSION_1
        fan.fan = index
        if self.call("nvmlDeviceGetFanSpeedRPM", ctypes.byref(fan)) != SUCCESS:
            return None
        return fan.speed

    def fan_speeds(self):
        """Returns the speed of each fan in revolutions per minute, or None for a fan that can't be read."""
        return [self.fan_speed(index) for index in range(self.fan_count)]

    def processes(self):
        """Returns the percentage of the GPU, of the encoder and of the decoder used by each process, from its
        latest sample, as (gpu, encoder, decoder) by PID."""
        size = SAMPLES
        for _attempt in (0, 1):
            count = ctypes.c_uint(size)
            samples = (_Sample * size)()
            result = self.call("nvmlDeviceGetProcessUtilization", samples, ctypes.byref(count), 0)
            if result != ERROR_INSUFFICIENT_SIZE:
                break
            size = max(size, count.value)
        if result != SUCCESS:
            return {}
        samples = samples[:min(count.value, size)]
        # Keep the samples of the last seconds only, so that a process that stopped using the GPU disappears from
        # the list. The timestamps are in the same clock as time(), but the newest one is used instead if they
        # don't match, in case the driver uses another clock.
        newest = max((sample.timestamp for sample in samples if sample.pid), default=0)
        now = int(time.time() * 1_000_000)
        reference = now if abs(now - newest) < SAMPLE_AGE else newest
        latest = {}
        for sample in samples:
            if not sample.pid or sample.timestamp < reference - SAMPLE_AGE:
                continue
            if sample.pid not in latest or sample.timestamp > latest[sample.pid].timestamp:
                latest[sample.pid] = sample
        return {pid: (min(100.0, float(sample.gpu)), min(100.0, float(sample.encoder)),
                      min(100.0, float(sample.decoder)))
                for pid, sample in latest.items()}

    def process_memory(self):
        """Returns the bytes of video memory used by each process, or nothing if it can't be read.

        A process can appear as both a graphics and a compute client, so the highest value of each one is kept."""
        memory = {}
        for prefix in ("Graphics", "Compute"):
            name = next((name for name in (f"nvmlDeviceGet{prefix}RunningProcesses_v2",
                                           f"nvmlDeviceGet{prefix}RunningProcesses")
                         if getattr(self.library, name, None) is not None), None)
            if name is None:
                continue
            for pid, used in self.read_running_processes(name).items():
                memory[pid] = max(memory.get(pid, 0), used)
        return memory

    def read_running_processes(self, name):
        """Returns the bytes of video memory used by each process, according to one of the queries of the running
        processes of NVML."""
        size = SAMPLES
        for _attempt in (0, 1):
            count = ctypes.c_uint(size)
            processes = (_Process * size)()
            result = self.call(name, ctypes.byref(count), processes)
            if result != ERROR_INSUFFICIENT_SIZE:
                break
            size = max(size, count.value)
        if result != SUCCESS:
            return {}
        found = {}
        for process in processes[:min(count.value, size)]:
            if process.pid and process.memory < UNKNOWN_MEMORY:
                found[process.pid] = max(found.get(process.pid, 0), process.memory)
        return found


class Nvml:
    """The NVIDIA GPUs of the machine."""

    def __init__(self, library):
        self.library = library
        self.devices = []

    def usage(self):
        """Returns a list of (percentage of use, name) for the GPUs that can be read."""
        readings = []
        for device in self.devices:
            value = device.usage()
            if value is not None:
                readings.append((value, device.name))
        return readings

    def temperature(self):
        """Returns the highest temperature of the GPUs in degrees Celsius, or None if it can't be read."""
        values = [value for device in self.devices if (value := device.temperature()) is not None]
        return max(values) if values else None

    def fans(self):
        """Returns a list of (name, speed) for each fan of the GPUs, in revolutions per minute."""
        speeds = self.fan_speeds()
        if len(speeds) == 1:
            return [(_("GPU"), speeds[0])]
        return [("%s %d" % (_("GPU"), index + 1), speed) for index, speed in enumerate(speeds)]

    def fan_speeds(self):
        """Returns the speed of each fan of the GPUs in revolutions per minute, or None for a fan that can't be
        read."""
        return [speed for device in self.devices for speed in device.fan_speeds()]

    def processes(self):
        """Returns the percentage of the GPU, of the encoder and of the decoder used by each process, and the bytes
        of video memory, as (gpu, video_memory, encoder, decoder) by PID.

        The busiest value of each kind is used when a process uses more than one GPU. A process that only holds
        video memory is included, with no usage."""
        usage = {}
        memory = {}
        for device in self.devices:
            for pid, values in device.processes().items():
                old = usage.get(pid)
                usage[pid] = values if old is None else tuple(max(a, b) for a, b in zip(old, values))
            for pid, used in device.process_memory().items():
                memory[pid] = memory.get(pid, 0) + used
        found = {}
        for pid in set(usage) | set(memory):
            gpu, encoder, decoder = usage.get(pid, (0.0, 0.0, 0.0))
            found[pid] = (gpu, memory.get(pid, 0), encoder, decoder)
        return found


def load():
    """Returns the NVIDIA GPUs, or None if NVML is not installed or if the driver is not in use."""
    try:
        library = ctypes.CDLL("libnvidia-ml.so.1")
    except OSError:
        return None
    if _call(library, "nvmlInit_v2") != SUCCESS:
        return None
    count = ctypes.c_uint()
    if _call(library, "nvmlDeviceGetCount_v2", ctypes.byref(count)) != SUCCESS:
        return None
    nvml = Nvml(library)
    for index in range(count.value):
        handle = ctypes.c_void_p()
        if _call(library, "nvmlDeviceGetHandleByIndex_v2", index, ctypes.byref(handle)) != SUCCESS:
            continue
        name = ctypes.create_string_buffer(96)
        _call(library, "nvmlDeviceGetName", handle, name, len(name))
        nvml.devices.append(Device(library, handle, name.value.decode("utf-8", "replace") or _("Unknown GPU")))
    return nvml if nvml.devices else None


def _call(library, name, *arguments):
    """Calls a function of the NVML library. Returns None if the installed driver doesn't have it."""
    function = getattr(library, name, None)
    return None if function is None else function(*arguments)


_lock = threading.Lock()
_read = False
_nvml = None


def get():
    """Returns the NVIDIA GPUs, or None if NVML is not available. It is loaded once, when first needed."""
    global _read, _nvml
    with _lock:
        if not _read:
            _nvml = load()
            _read = True
    return _nvml
