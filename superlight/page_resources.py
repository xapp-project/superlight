import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango

from superlight import MARGIN, _, format_size
from superlight.common_graphs import (DISK_SERIES, NETWORK_SERIES, SERIES, Alignment, CoreGraph, CpuGraph, DiskGraph,
                              FanGraph, NetworkGraph, TemperatureGraph, build_legend, draw_swatch, format_rate,
                              set_legend)


class ResourcesPage(Gtk.ScrolledWindow):
    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.cpu_graph = CpuGraph()
        self.core_graph = CoreGraph()
        self.network_graph = NetworkGraph()
        self.disk_graph = DiskGraph()
        self.temperature_graph = TemperatureGraph()
        self.graphs = [self.cpu_graph, self.core_graph, self.network_graph, self.disk_graph]
        if self.temperature_graph.available():
            self.graphs.append(self.temperature_graph)
        self.fan_graph = FanGraph()
        if self.fan_graph.available():
            self.graphs.append(self.fan_graph)
        alignment = Alignment()
        for graph in self.graphs:
            graph.alignment = alignment
            alignment.graphs.append(graph)
        for graph in self.graphs[1:]:
            graph.record_after = self.cpu_graph.record_after

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=MARGIN)
        self.legend_labels = []
        legend = build_legend(2, 24, [(rgb, _("Collecting data…")) for _name, rgb in SERIES],
                             self.legend_labels)
        page.pack_start(self.section("overall", _("Overall usage"), self.cpu_graph, legend), False, True, 0)
        self.core_labels = []
        legend = build_legend(8, 16, [(rgb, f"{name}: —") for name, rgb in self.core_graph.series],
                             self.core_labels)
        page.pack_start(self.section("cores", _("CPU cores"), self.core_graph, legend), False, True, 0)
        self.network_labels = []
        legend = self.rate_legend(NETWORK_SERIES, ((_("Receiving"), _("Total Received")),
                                                   (_("Sending"), _("Total Sent"))), self.network_labels)
        page.pack_start(self.section("network", _("Network"), self.network_graph, legend), False, True, 0)
        self.disk_labels = []
        legend = self.rate_legend(DISK_SERIES, ((_("Reading"), _("Total Read")),
                                                (_("Writing"), _("Total Written"))), self.disk_labels)
        page.pack_start(self.section("disk", _("Disk"), self.disk_graph, legend), False, True, 0)
        self.temperature_labels = []
        if self.temperature_graph.available():
            legend = build_legend(3, 24, [(rgb, _("Collecting data…")) for _name, rgb in self.temperature_graph.series],
                                 self.temperature_labels)
            page.pack_start(self.section("temperature", _("Temperature"), self.temperature_graph, legend),
                            False, True, 0)
        self.fan_labels = []
        if self.fan_graph.available():
            legend = build_legend(3, 24, [(rgb, _("Collecting data…")) for _name, rgb in self.fan_graph.series],
                                 self.fan_labels)
            page.pack_start(self.section("fans", _("Fan Speed"), self.fan_graph, legend), False, True, 0)
        self.add(page)

        self.ticks = [(graph, graph.add_tick_callback(graph.animate)) for graph in self.graphs]

    def stop(self):
        for graph, tick in self.ticks:
            graph.remove_tick_callback(tick)

    def section(self, key, title, graph, legend):
        """Returns an expander with a title, which hides or shows a graph and its legend. The state is saved."""
        label = Gtk.Label(label=title, xalign=0)
        label.set_attributes(Pango.AttrList.from_string("0 -1 weight bold"))
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.pack_start(graph, True, True, 0)
        content.pack_start(legend, False, False, 0)
        expander = Gtk.Expander(label_widget=label, expanded=key in self.settings.get_strv("expanded-graphs"))
        expander.add(content)
        expander.set_vexpand(expander.get_expanded())
        expander.connect("notify::expanded", self.on_expanded, key)
        return expander

    def on_expanded(self, expander, _param, key):
        # A collapsed section must not keep the space that the others could use
        expander.set_vexpand(expander.get_expanded())
        expanded = [name for name in self.settings.get_strv("expanded-graphs") if name != key]
        if expander.get_expanded():
            expanded.append(key)
        self.settings.set_strv("expanded-graphs", expanded)

    def rate_legend(self, series, names, labels):
        """Returns a legend with two lines for each series: its rate and its total, with the values on the right.
        The labels of the values are added to labels, as a pair for each series."""
        legend = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=2,
                             column_spacing=24)
        for (_name, rgb), (rate_name, total_name) in zip(series, names):
            grid = Gtk.Grid(column_spacing=12, row_spacing=2)
            swatch = Gtk.DrawingArea(valign=Gtk.Align.CENTER)
            swatch.set_size_request(20, 12)
            swatch.connect("draw", draw_swatch, rgb)
            grid.attach(swatch, 0, 0, 1, 2)
            rate = Gtk.Label(label="", xalign=1)
            total = Gtk.Label(label="", xalign=1)
            for row, (name, value) in enumerate(((rate_name, rate), (total_name, total))):
                grid.attach(Gtk.Label(label=name, xalign=0), 1, row, 1, 1)
                grid.attach(value, 2, row, 1, 1)
            legend.add(grid)
            labels.append((rate, total))
        return legend

    def refresh(self):
        for graph in self.graphs:
            graph.refresh()
        widest_core = max((name for name, _rgb in self.core_graph.series), key=len)
        for (name, _rgb), label, value in zip(
                self.core_graph.series, self.core_labels, self.core_graph.current_usage):
            set_legend(label, _("%(name)s: %(percent).0f%%") % {"name": name, "percent": value}
                            if value is not None else _("%s: Offline") % name,
                            _("%(name)s: %(percent).0f%%") % {"name": widest_core, "percent": 100})
        cpu, gpu, memory, swap = self.cpu_graph.current_usage
        used, total, swap_used, swap_total = self.cpu_graph.memory_usage
        labels = self.legend_labels
        set_legend(labels[0], _("CPU: %.0f%%") % cpu,
                        _("CPU: %.0f%%") % 100)
        if gpu is not None:
            set_legend(labels[1], _("GPU: %(percent).0f%% (%(name)s)") % {
                                "percent": gpu, "name": self.cpu_graph.gpu_usage[1]},
                            _("GPU: %(percent).0f%% (%(name)s)") % {
                                "percent": 100, "name": self.cpu_graph.gpu_usage[1]})
        else:
            set_legend(labels[1], _("GPU: Usage unavailable from driver"))
        set_legend(labels[2], _("Memory: %(percent).0f%% (%(used)s of %(total)s)") % {
                            "percent": memory, "used": format_size(used), "total": format_size(total)},
                        _("Memory: %(percent).0f%% (%(used)s of %(total)s)") % {
                            "percent": 100, "used": format_size(total), "total": format_size(total)})
        if swap_total:
            set_legend(labels[3], _("Swap: %(percent).0f%% (%(used)s of %(total)s)") % {
                                "percent": swap, "used": format_size(swap_used),
                                "total": format_size(swap_total)},
                            _("Swap: %(percent).0f%% (%(used)s of %(total)s)") % {
                                "percent": 100, "used": format_size(swap_total),
                                "total": format_size(swap_total)})
        else:
            set_legend(labels[3], _("Swap: Not configured"))
        for label, text, unavailable, value in zip(
                self.temperature_labels,
                (_("CPU: %.0f °C"), _("GPU: %.0f °C"), _("Disk: %.0f °C")),
                (_("CPU: Unavailable"), _("GPU: Unavailable"), _("Disk: Unavailable")),
                self.temperature_graph.current_usage):
            set_legend(label, text % value if value is not None else unavailable, text % 100)
        if self.fan_labels:
            widest = max((name for name, _rgb in self.fan_graph.series), key=len)
            for (name, _rgb), label, speed in zip(self.fan_graph.series, self.fan_labels,
                                                  self.fan_graph.current_usage):
                set_legend(label, _("%(name)s: %(speed)d RPM") % {"name": name, "speed": speed}
                                if speed is not None else _("%s: Unavailable") % name,
                                _("%(name)s: %(speed)d RPM") % {"name": widest, "speed": 9999})
        self.show_rates(self.network_graph, self.network_labels)
        self.show_rates(self.disk_graph, self.disk_labels)

    def show_rates(self, graph, labels):
        for (rate_label, total_label), rate, total in zip(labels, graph.current_usage, graph.totals):
            set_legend(rate_label, format_rate(rate), format_rate(999_900_000))
            set_legend(total_label, format_size(total), format_size(999_900_000_000))
