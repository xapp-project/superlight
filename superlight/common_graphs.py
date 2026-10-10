import colorsys
import os
import re

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import GLib, Gtk, PangoCairo

from superlight import _, format_size, rgb
from superlight.common_proc import (TEMPERATURE_CHIPS, gpu_name, read_core_times, read_cpu_times, read_disk_bytes,
                                    read_fans, read_memory_usage, read_network_bytes, read_temperatures, read_text)

SERIES = (("CPU", rgb("#3373D9")),
          ("GPU", rgb("#9959BF")),
          ("Memory", rgb("#D94040")),
          ("Swap", rgb("#1A8C52")))
NETWORK_SERIES = (("Received", rgb("#3373D9")), ("Sent", rgb("#E6801A")))
DISK_SERIES = (("Read", rgb("#3373D9")), ("Written", rgb("#E6801A")))
TEMPERATURE_SERIES = (("CPU", rgb("#3373D9")), ("GPU", rgb("#9959BF")), ("Disk", rgb("#E6801A")))
MINIMUM_RATE = 100_000   # smallest maximum of the vertical scale of a RateGraph, in bytes per second
MINIMUM_SPEED = 1000     # smallest maximum of the vertical scale of a FanGraph, in RPM
IDLE_RATE = 1000         # RateGraph treats rates below this value (bytes per second) as zero


def time_to_x(age_seconds, width, span=60):
    """Converts the age of a reading to an x coordinate.

    age_seconds: how long ago the reading was made.
    width: width of the plotting area, in pixels.
    span: the number of seconds the plotting area covers.
    Returns width for age 0 (right edge) and 0 for age span (left edge).
    """
    return width * (1 - age_seconds / span)


def format_rate(rate):
    """Returns a rate in bytes per second as text, such as "12 MB/s". Rates under IDLE_RATE give "< 1 kB/s"."""
    if rate < IDLE_RATE:
        return _("< 1 kB/s")
    # "100.0 kB/s" becomes "100 kB/s"
    return _("%s/s") % re.sub(r"[.,]0(?=\D|$)", "", format_size(rate))


def nice_maximum(peak, minimum):
    """Returns a round maximum for a vertical scale.

    The result is the smallest of minimum x (1, 2, 5, 10, 20, 50, 100...) that is not smaller than peak.
    """
    step = minimum
    while True:
        for factor in (1, 2, 5):
            if peak <= step * factor:
                return step * factor
        step *= 10


class Alignment:
    """Keeps the plotting areas of several graphs aligned.

    Graphs that share one Alignment (set graph.alignment, and add the graph to Alignment.graphs) reserve the same
    space for their labels: the width of the widest vertical-axis label (axis_width) and of the widest time label
    (time_label_width) of any of them.
    """

    def __init__(self):
        self.axis_width = 0
        self.time_label_width = 0
        self.graphs = []


class Graph(Gtk.DrawingArea):
    """A scrolling line graph of the last minute of one or more series, drawn with Cairo.

    series is a sequence of (name, (r, g, b)) with colors from 0 to 1. Call add_sample() with one value per series
    (None for no reading) to add a reading. Subclasses read the values in refresh() and can override axis_maximum()
    and format_tick() to set the scale and its labels. The default scale is 0 to 100 (percent).
    """

    tick_options = (2, 1)   # numbers of parts the vertical scale can be divided in. The first one that fits is used.
    span = 60               # seconds shown on the time axis
    divisions = 6           # number of parts of the time axis
    hold_to_now = True      # extend the last reading to the right edge, until a new one comes

    def __init__(self, series):
        super().__init__()
        self.series = series
        self.samples = []
        self.record_after = GLib.get_monotonic_time() + 3_000_000
        self.current_usage = (0,) * len(series)
        self.alignment = Alignment()
        self.alignment.graphs.append(self)
        self.plot_margins = (0, 0)   # the space left and right of the plotting area, known once the graph is drawn
        self.set_size_request(-1, 80)
        self.set_vexpand(True)
        self.connect("draw", self.draw_graph)

    def add_sample(self, values):
        """Adds a reading, a tuple with a value for each series, and redraws. Readings older than span are dropped."""
        now = GLib.get_monotonic_time()
        self.current_usage = values
        if now >= self.record_after:
            self.samples.append((now, values))
        # Keep one reading beyond the left edge, so that the line reaches it
        while len(self.samples) > 2 and self.samples[1][0] < now - 60_000_000:
            self.samples.pop(0)
        self.queue_draw()

    def animate(self, _widget, _frame_clock):
        """Tick callback (Gtk.Widget.add_tick_callback) that redraws on every frame, to scroll smoothly."""
        self.queue_draw()
        return True

    def axis_maximum(self):
        """Returns the top value of the vertical scale. Subclasses override it."""
        return 100

    def largest_sample(self):
        """Returns the largest value among the stored readings, or 0 if there are none."""
        largest = 0
        for _timestamp, values in self.samples:
            for value in values:
                if value is not None and value > largest:
                    largest = value
        return largest

    def format_tick(self, value):
        """Returns the label for a value on the vertical scale. Subclasses override it."""
        return "%d%%" % round(value)

    def format_age(self, seconds):
        """Returns the label of a vertical line on the time axis, for a reading that many seconds old."""
        return _("1 min") if seconds == 60 else _("%d secs") % seconds

    def draw_graph(self, _widget, cr):
        """Handler of the draw signal: draws the axes, the labels and the lines of all the series."""
        allocation = self.get_allocation()
        maximum = self.axis_maximum()
        ages = [self.span * step / self.divisions for step in range(self.divisions, -1, -1)]
        label = self.create_pango_layout("")
        time_label_width = label_height = 0
        for age in ages[:-1]:
            label.set_text(self.format_age(age), -1)
            width, label_height = label.get_pixel_size()
            time_label_width = max(time_label_width, width)
        top_margin = label_height / 2 + 4
        height = max(1, allocation.height - top_margin - label_height - 16)
        # Use the first tick_options value that leaves at least two lines of text between the labels
        steps = next((steps for steps in self.tick_options if height / steps >= 2 * label_height),
                     self.tick_options[-1])
        ticks = [maximum * step / steps for step in range(steps + 1)]
        axis_width = 0
        for tick in ticks:
            label.set_text(self.format_tick(tick), -1)
            axis_width = max(axis_width, label.get_pixel_size()[0])
        # Grow the shared label sizes if needed, and redraw the other graphs that use them
        alignment = self.alignment
        if time_label_width > alignment.time_label_width or axis_width > alignment.axis_width:
            alignment.time_label_width = max(time_label_width, alignment.time_label_width)
            alignment.axis_width = max(axis_width, alignment.axis_width)
            for graph in alignment.graphs:
                if graph is not self:
                    graph.queue_draw()
        left_margin = alignment.time_label_width / 2 + 4
        right_margin = alignment.axis_width + 16
        self.plot_margins = (round(left_margin), round(right_margin))
        width = max(1, allocation.width - left_margin - right_margin)
        cr.save()
        cr.translate(left_margin, top_margin)
        color = self.get_style_context().get_color(self.get_state_flags())
        background = cairo.LinearGradient(0, 0, 0, height)
        background.add_color_stop_rgba(0, color.red, color.green, color.blue, 0.04)
        background.add_color_stop_rgba(1, color.red, color.green, color.blue, 0.0)
        cr.rectangle(0, 0, width, height)
        cr.set_source(background)
        cr.fill()
        for tick in ticks:
            y = height * (1 - tick / maximum)
            cr.set_source_rgba(color.red, color.green, color.blue, 0.12)
            cr.rectangle(0, y, width, 1)
            cr.fill()
            if tick == 0:
                continue
            label.set_text(self.format_tick(tick), -1)
            cr.set_source_rgba(color.red, color.green, color.blue, color.alpha)
            cr.move_to(width + 8, y - label_height / 2)
            PangoCairo.show_layout(cr, label)

        # Vertical lines and time labels
        for seconds in ages:
            x = time_to_x(seconds, width, self.span)
            cr.set_source_rgba(color.red, color.green, color.blue, 0.12)
            cr.rectangle(x, 0, 1, height)
            cr.fill()
            if seconds == 0:
                continue
            label.set_text(self.format_age(seconds), -1)
            label_width = label.get_pixel_size()[0]
            cr.set_source_rgba(color.red, color.green, color.blue, color.alpha)
            cr.move_to(x - label_width / 2, height + 8)
            PangoCairo.show_layout(cr, label)

        now = GLib.get_monotonic_time()
        cr.save()
        cr.rectangle(0, 0, width, height)
        cr.clip()
        paths = []
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        for index, (_name, rgb) in enumerate(self.series):
            if not self.samples:
                break
            if self.hold_to_now and self.samples[-1][1][index] is None:
                continue
            # A missing reading (None) splits the line in pieces
            runs = [[]]
            for timestamp, values in self.samples:
                if values[index] is None:
                    if runs[-1]:
                        runs.append([])
                    continue
                runs[-1].append((time_to_x((now - timestamp) / 1_000_000, width, self.span),
                                 height * (1 - min(values[index], maximum) / maximum)))
            if self.hold_to_now and runs[-1]:
                runs[-1].append((width, runs[-1][-1][1]))
            for points in runs:
                if not points:
                    continue
                paths.append((points, rgb))
                cr.move_to(points[0][0], height)
                for x, y in points:
                    cr.line_to(x, y)
                cr.line_to(points[-1][0], height)
                cr.close_path()
                gradient = cairo.LinearGradient(0, 0, 0, height)
                gradient.add_color_stop_rgba(0, *rgb, 0.20)
                gradient.add_color_stop_rgba(1, *rgb, 0.03)
                cr.set_source(gradient)
                cr.fill()
        # Draw the outlines after all the fills, so that no fill hides an outline
        for points, rgb in paths:
            cr.move_to(*points[0])
            for x, y in points[1:]:
                cr.line_to(x, y)
            if len(points) == 1:
                cr.line_to(*points[0])
            cr.set_source_rgb(*rgb)
            cr.set_line_width(2)
            cr.stroke()
        cr.restore()
        cr.restore()
        return False


class CpuGraph(Graph):
    """Usage in percent of the CPU, the GPU, the memory and the swap. Call refresh() regularly to add a reading."""

    tick_options = (5, 2, 1)

    def __init__(self):
        super().__init__(SERIES)
        self.memory_usage = read_memory_usage()
        self.gpu_usage = (None, None)
        self.current_usage = (0, None, 0, 0)
        self.previous = read_cpu_times()

    def refresh(self):
        idle, total = read_cpu_times()
        old_idle, old_total = self.previous
        difference = total - old_total
        usage = 0 if difference == 0 else 100 * (1 - (idle - old_idle) / difference)
        self.previous = (idle, total)
        self.memory_usage = read_memory_usage()
        memory_used, memory_total, swap_used, swap_total = self.memory_usage
        # Read the busy percentage of each GPU, and keep the busiest one with its name
        readings = []
        for card in os.listdir("/sys/class/drm"):
            if not card.startswith("card") or not card[4:].isdigit():
                continue
            path = os.path.realpath(f"/sys/class/drm/{card}/device")
            try:
                with open(f"{path}/gpu_busy_percent") as source:
                    value = float(source.read())
                if 0 <= value <= 100:
                    readings.append((value, gpu_name(path)))
            except (OSError, ValueError):
                continue
        self.gpu_usage = max(readings) if readings else (None, None)
        self.add_sample((max(0, min(100, usage)),
                         self.gpu_usage[0],
                         100 * memory_used / memory_total if memory_total else 0,
                         100 * swap_used / swap_total if swap_total else 0))


class CoreGraph(Graph):
    """Usage in percent of each CPU core, one series per core. Call refresh() regularly to add a reading."""

    tick_options = (5, 2, 1)

    def __init__(self):
        self.previous_cores = read_core_times()
        self.core_names = sorted(self.previous_cores, key=lambda name: int(name[3:]))
        super().__init__(tuple((f"CPU{int(name[3:]) + 1}", colorsys.hsv_to_rgb(
            (index * 0.61803398875) % 1, 0.65, 0.75))
            for index, name in enumerate(self.core_names)))

    def refresh(self):
        current = read_core_times()
        values = []
        for name in self.core_names:
            if name not in current or name not in self.previous_cores:
                values.append(None)
                continue
            idle, total = current[name]
            old_idle, old_total = self.previous_cores[name]
            delta = total - old_total
            values.append(max(0, min(100, 100 * (1 - (idle - old_idle) / delta)))
                          if delta > 0 else 0)
        self.previous_cores = current
        self.add_sample(tuple(values))


class RateGraph(Graph):
    """Base class for graphs of a rate of bytes per second, calculated from counters that only grow.

    read_counters is a function that returns a tuple with the current total for each series. refresh() calculates
    the rate since the previous call. The vertical scale follows the highest rate shown.
    """

    def __init__(self, series, read_counters):
        super().__init__(series)
        self.read_counters = read_counters
        self.totals = read_counters()   # latest totals returned by read_counters
        self.previous_time = GLib.get_monotonic_time()

    def refresh(self):
        now = GLib.get_monotonic_time()
        totals = self.read_counters()
        seconds = max(0.000001, (now - self.previous_time) / 1_000_000)
        # A total can decrease when a device disappears. Use 0 instead of a negative rate.
        rates = tuple(max(0, new - old) / seconds for new, old in zip(totals, self.totals))
        rates = tuple(rate if rate >= IDLE_RATE else 0 for rate in rates)
        self.totals = totals
        self.previous_time = now
        self.add_sample(rates)

    def axis_maximum(self):
        return nice_maximum(self.largest_sample(), MINIMUM_RATE)

    def format_tick(self, value):
        return format_rate(value)


class NetworkGraph(RateGraph):
    """Bytes per second received and sent by all the network interfaces except lo."""

    def __init__(self):
        super().__init__(NETWORK_SERIES, read_network_bytes)


class DiskGraph(RateGraph):
    """Bytes per second read from and written to the whole disks."""

    def __init__(self):
        super().__init__(DISK_SERIES, read_disk_bytes)


class TemperatureGraph(Graph):
    """Temperature in degrees Celsius of the CPU, the GPU and the disks, read from /sys/class/hwmon, on a scale of
    0 to 100. Use available() to know if any sensor was found."""

    tick_options = (5, 2, 1)

    def __init__(self):
        # Files to read, by kind of sensor (see TEMPERATURE_CHIPS). The highest temperature of a kind is shown.
        self.sensors = {kind: [] for kind in TEMPERATURE_CHIPS}
        for chip in sorted(os.listdir("/sys/class/hwmon")):
            directory = f"/sys/class/hwmon/{chip}"
            try:
                name = read_text(f"{directory}/name")
                inputs = sorted(f for f in os.listdir(directory) if f.startswith("temp") and f.endswith("_input"))
                labels = {}
                for f in inputs:
                    label_file = f"{directory}/{f[:-len('input')]}label"
                    labels[f] = read_text(label_file) if os.path.exists(label_file) else ""
            except OSError:
                continue
            for kind, chips in TEMPERATURE_CHIPS.items():
                if name not in chips or not inputs:
                    continue
                # Use the sensor with a preferred label, or else the first one of the chip
                chosen = inputs[0]
                for label in chips[name]:
                    matches = [f for f in inputs if labels[f] == label]
                    if matches:
                        chosen = matches[0]
                        break
                self.sensors[kind].append(f"{directory}/{chosen}")
        super().__init__(TEMPERATURE_SERIES)
        self.current_usage = read_temperatures(self.sensors)

    def available(self):
        """Returns True if at least one temperature could be read."""
        return any(value is not None for value in self.current_usage)

    def refresh(self):
        self.add_sample(read_temperatures(self.sensors))

    def format_tick(self, value):
        return _("%d °C") % round(value)


class FanGraph(Graph):
    """Speed in revolutions per minute of each fan found in /sys/class/hwmon, one series per fan. The vertical
    scale follows the highest speed. Use available() to know if any fan was found."""

    def __init__(self):
        # (name, file) for each fan. The name is the label of the fan, or "Fan <number>" if it has none.
        self.fans = []
        for chip in sorted(os.listdir("/sys/class/hwmon"), key=lambda name: int(name[5:] or 0)):
            directory = f"/sys/class/hwmon/{chip}"
            try:
                inputs = sorted(f for f in os.listdir(directory) if f.startswith("fan") and f.endswith("_input"))
            except OSError:
                continue
            for f in inputs:
                try:
                    label = read_text(f"{directory}/{f[:-len('input')]}label")
                except OSError:
                    label = ""
                self.fans.append((label or _("Fan %d") % (len(self.fans) + 1), f"{directory}/{f}"))
        super().__init__(tuple((name, colorsys.hsv_to_rgb((index * 0.61803398875 + 0.55) % 1, 0.65, 0.75))
                               for index, (name, _path) in enumerate(self.fans)))
        self.current_usage = read_fans(self.fans)

    def available(self):
        """Returns True if at least one fan was found."""
        return bool(self.fans)

    def refresh(self):
        self.add_sample(read_fans(self.fans))

    def axis_maximum(self):
        return nice_maximum(self.largest_sample(), MINIMUM_SPEED)

    def format_tick(self, value):
        return _("%d RPM") % round(value)


def draw_swatch(widget, cr, rgb):
    """Draw handler that draws a horizontal line of the color rgb, (r, g, b) from 0 to 1, across the widget."""
    cr.set_source_rgb(*rgb)
    cr.set_line_width(3)
    cr.move_to(0, widget.get_allocated_height() / 2)
    cr.line_to(widget.get_allocated_width(), widget.get_allocated_height() / 2)
    cr.stroke()
    return False


def build_legend(columns, spacing, entries, labels):
    """Returns a Gtk.FlowBox with a colored line and a label for each (rgb, text) in entries.

    columns: maximum number of entries per row. spacing: space between columns, in pixels.
    The labels are appended to the list labels, in order, so that the caller can update their text.
    """
    legend = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=columns,
                         column_spacing=spacing)
    for rgb, text in entries:
        row = Gtk.Box(spacing=8)
        swatch = Gtk.DrawingArea()
        swatch.set_size_request(20, 12)
        swatch.connect("draw", draw_swatch, rgb)
        row.pack_start(swatch, False, False, 0)
        label = Gtk.Label(label=text, xalign=0)
        row.pack_start(label, False, False, 0)
        legend.add(row)
        labels.append(label)
    return legend


def set_legend(label, text, widest=None):
    """Sets the text of a label and never makes the label narrower than before.

    The label is given a minimum width: the widest of the texts it showed, and of widest if given. This stops the
    widgets next to it from moving when the text changes.
    """
    label.set_text(text)
    layout = label.create_pango_layout("")
    width = 0
    for candidate in (text, widest):
        if candidate:
            layout.set_text(candidate, -1)
            width = max(width, layout.get_pixel_size()[0])
    if width > label.get_size_request()[0]:
        label.set_size_request(width, -1)
