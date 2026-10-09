import gettext
import re
import warnings

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("UPowerGlib", "1.0")
from gi.repository import GLib, Gtk, Pango, UPowerGlib

from superlight import MARGIN, _, format_decimal, rgb
from superlight.common_graphs import Graph, build_legend, nice_maximum

Kind = UPowerGlib.DeviceKind
State = UPowerGlib.DeviceState
Technology = UPowerGlib.DeviceTechnology

KINDS = {Kind.BATTERY: _("Battery"), Kind.UPS: _("Uninterruptible Power Supply"), Kind.MOUSE: _("Mouse"),
         Kind.KEYBOARD: _("Keyboard"), Kind.GAMING_INPUT: _("Game Controller"), Kind.PHONE: _("Phone"),
         Kind.TABLET: _("Tablet"), Kind.PEN: _("Pen"), Kind.TOUCHPAD: _("Touchpad"),
         Kind.HEADSET: _("Headset"), Kind.HEADPHONES: _("Headphones"), Kind.SPEAKERS: _("Speakers"),
         Kind.MEDIA_PLAYER: _("Media Player"), Kind.PDA: _("Personal Digital Assistant"),
         Kind.COMPUTER: _("Computer"), Kind.REMOTE_CONTROL: _("Remote Control")}
STATES = {State.CHARGING: _("Charging"), State.DISCHARGING: _("Discharging"), State.EMPTY: _("Empty"),
          State.FULLY_CHARGED: _("Fully charged"), State.PENDING_CHARGE: _("Waiting to charge"),
          State.PENDING_DISCHARGE: _("Waiting to discharge")}
TECHNOLOGIES = {Technology.LITHIUM_ION: _("Lithium Ion"), Technology.LITHIUM_POLYMER: _("Lithium Polymer"),
                Technology.LITHIUM_IRON_PHOSPHATE: _("Lithium Iron Phosphate"),
                Technology.LEAD_ACID: _("Lead acid"), Technology.NICKEL_CADMIUM: _("Nickel Cadmium"),
                Technology.NICKEL_METAL_HYDRIDE: _("Nickel metal hydride")}
# The series of the history graph, with their colors
HISTORY_SERIES = ((_("Discharging"), rgb("#3373D9")), (_("Charging"), rgb("#1A8C52")),
                  (_("Idle"), rgb("#8C8C8C")))
DISCHARGING, CHARGING, IDLE = range(3)
HISTORY_SERIES_OF_STATE = {State.DISCHARGING: DISCHARGING, State.PENDING_DISCHARGE: DISCHARGING,
                           State.EMPTY: DISCHARGING, State.CHARGING: CHARGING, State.PENDING_CHARGE: CHARGING}
# The periods of history that can be shown, as (key used in the settings, seconds, vertical lines, title)
SPANS = (("30m", 1800, 6, _("30 minutes")), ("3h", 10800, 6, _("3 hours")), ("8h", 28800, 8, _("8 hours")),
         ("1d", 86400, 12, _("1 day")), ("1w", 604800, 7, _("1 week")))
KINDS_OF_HISTORY = (("charge", _("Charge")), ("rate", _("Power")))
HISTORY_POINTS = 150
HISTORY_RELOAD_SECONDS = 30
MINIMUM_POWER = 1   # the smallest top of the scale of the power graph, in watts


def format_duration(seconds):
    minutes = max(1, round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(gettext.ngettext("%d hour", "%d hours", hours) % hours)
    if minutes:
        parts.append(gettext.ngettext("%d minute", "%d minutes", minutes) % minutes)
    return " ".join(parts)


def format_number(value, unit):
    """Returns a number with its unit, without a useless ".0": 12.0 Wh is 12 Wh, 39.5 Wh is 40 Wh, 7.14 W is 7.1 W."""
    return re.sub(r"[.,]0(?=\D|$)", "", format_decimal(value)) + "\u00a0" + unit


class PowerMonitor:
    """Keeps the list of the devices that UPower says have a battery. changed is called when a device is added or
    removed."""

    def __init__(self, changed):
        try:
            self.client = UPowerGlib.Client.new()
        except GLib.Error:
            self.client = None
            return
        self.client.connect("device-added", lambda *_: changed())
        self.client.connect("device-removed", lambda *_: changed())

    def devices(self):
        """Returns the batteries, with the battery of the computer first."""
        if self.client is None:
            return []
        # get_devices2() would be better, but the devices it returns crash PyGObject when they are used
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            found = self.client.get_devices()
        devices = [device for device in found
                   if device.props.is_present and device.props.kind not in (Kind.LINE_POWER, Kind.UNKNOWN, Kind.MONITOR)]
        return sorted(devices, key=lambda device: (device.props.kind != Kind.BATTERY, device.props.model or ""))


class HistoryGraph(Graph):
    """A graph of the charge or the power of a battery over time, from the history that UPower keeps."""

    hold_to_now = False   # the line stops at the last reading

    def __init__(self):
        super().__init__(HISTORY_SERIES)
        self.set_size_request(-1, 160)
        self.kind = "charge"
        self.set_no_show_all(True)   # hidden when there is no history

    def set_history(self, items, kind, span, divisions):
        self.kind = kind
        self.span = span
        self.divisions = divisions
        self.tick_options = (5, 2, 1) if kind == "charge" else (2, 1)
        now = GLib.get_monotonic_time()
        now_epoch = GLib.get_real_time() / 1_000_000
        samples = []
        previous = None
        for item in sorted(items or [], key=lambda item: item.get_time()):
            state = item.get_state()
            if state == State.UNKNOWN:
                continue
            index = HISTORY_SERIES_OF_STATE.get(state, IDLE)
            values = [None] * len(HISTORY_SERIES)
            values[index] = item.get_value()
            if previous is not None and previous != index:
                # Extend the previous line to this point, so that the lines touch
                values[previous] = item.get_value()
            samples.append((now - round((now_epoch - item.get_time()) * 1_000_000), tuple(values)))
            previous = index
        self.samples = samples
        self.queue_draw()

    def axis_maximum(self):
        if self.kind == "charge":
            return 100
        return nice_maximum(self.largest_sample(), MINIMUM_POWER)

    def format_tick(self, value):
        if self.kind == "charge":
            return "%d%%" % round(value)
        return format_number(value, "W")

    def format_age(self, seconds):
        if seconds % 86400 == 0:
            return _("%d d") % (seconds // 86400)
        if seconds >= 3600:
            return _("%s h") % re.sub(r"[.,]0(?=\D|$)", "", format_decimal(seconds / 3600))
        return _("%d min") % round(seconds / 60)


class BatteryPage(Gtk.Box):
    """The batteries of the computer and of connected devices: their state now, and their history."""

    def __init__(self, settings):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, border_width=MARGIN)
        self.settings = settings
        self.devices = []
        self.device = None
        self.watches = []
        self.history_loaded = 0

        self.device_list = Gtk.ListBox()
        self.device_list.connect("row-selected", self.on_row_selected)
        self.device_scroll = Gtk.ScrolledWindow(no_show_all=True, shadow_type=Gtk.ShadowType.IN)
        self.device_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.device_scroll.set_size_request(200, -1)
        self.device_scroll.add(self.device_list)
        self.pack_start(self.device_scroll, False, False, 0)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=12)
        self.percentage = Gtk.Label(xalign=0)
        self.percentage.set_markup("<span size='xx-large' weight='bold'> </span>")
        self.status = Gtk.Label(xalign=0, wrap=True)
        self.level = Gtk.LevelBar(min_value=0, max_value=100)
        content.pack_start(self.percentage, False, False, 0)
        content.pack_start(self.level, False, False, 0)
        content.pack_start(self.status, False, False, 0)

        self.grid = Gtk.Grid(column_spacing=24, row_spacing=4)
        self.rows = {}
        for name, title in (("vendor", _("Vendor")), ("model", _("Model")), ("technology", _("Technology")),
                            ("energy", _("Energy")), ("energy-full", _("Energy when full")),
                            ("energy-full-design", _("Energy (design)")), ("capacity", _("Capacity")),
                            ("rate", _("Power")), ("voltage", _("Voltage")),
                            ("charge-cycles", _("Charge cycles")), ("temperature", _("Temperature"))):
            title_label = Gtk.Label(label=title, xalign=0, no_show_all=True)
            title_label.get_style_context().add_class("dim-label")
            value_label = Gtk.Label(xalign=0, no_show_all=True, selectable=True)
            row = len(self.rows)
            self.grid.attach(title_label, 0, row, 1, 1)
            self.grid.attach(value_label, 1, row, 1, 1)
            self.rows[name] = (title_label, value_label)
        content.pack_start(self.grid, False, False, 0)

        controls = Gtk.Box(spacing=12)
        self.kind_combo = Gtk.ComboBoxText()
        for key, title in KINDS_OF_HISTORY:
            self.kind_combo.append(key, title)
        self.kind_combo.set_active_id(settings.get_string("battery-history-type"))
        self.span_combo = Gtk.ComboBoxText()
        for key, _seconds, _divisions, title in SPANS:
            self.span_combo.append(key, title)
        self.span_combo.set_active_id(settings.get_string("battery-history-span"))
        controls.pack_start(self.kind_combo, False, False, 0)
        controls.pack_start(self.span_combo, False, False, 0)
        content.pack_start(controls, False, False, 0)
        self.graph = HistoryGraph()
        content.pack_start(self.graph, True, True, 0)
        content.pack_start(build_legend(3, 24, [(rgb, name) for name, rgb in HISTORY_SERIES], []),
                           False, False, 0)
        self.no_history = Gtk.Label(label=_("No history is available for this device yet"), no_show_all=True)
        self.no_history.get_style_context().add_class("dim-label")
        content.pack_start(self.no_history, False, False, 0)
        self.kind_combo.connect("changed", self.on_history_choice_changed)
        self.span_combo.connect("changed", self.on_history_choice_changed)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(content)
        self.pack_start(scroll, True, True, 0)

    def set_devices(self, devices):
        """Shows these devices, and keeps the same one selected if it is still there."""
        for device, handler in self.watches:
            device.disconnect(handler)
        self.watches = [(device, device.connect("notify", self.on_device_changed)) for device in devices]
        self.devices = devices
        for row in self.device_list.get_children():
            self.device_list.remove(row)
        for device in devices:
            row = Gtk.ListBoxRow()
            row.device = device
            box = Gtk.Box(spacing=8, margin=8)
            row.image = Gtk.Image(icon_size=Gtk.IconSize.LARGE_TOOLBAR)
            row.name = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
            row.percentage = Gtk.Label()
            row.percentage.get_style_context().add_class("dim-label")
            box.pack_start(row.image, False, False, 0)
            box.pack_start(row.name, True, True, 0)
            box.pack_start(row.percentage, False, False, 0)
            row.add(box)
            self.device_list.add(row)
        self.device_list.show_all()
        self.device_scroll.set_visible(len(devices) > 1)
        self.update_rows()
        current = next((row for row in self.device_list.get_children() if row.device is self.device),
                       self.device_list.get_row_at_index(0))
        self.device_list.select_row(current)

    def update_rows(self):
        for row in self.device_list.get_children():
            props = row.device.props
            name = KINDS.get(props.kind, props.model or _("Battery"))
            # Name a battery by its kind. Name other devices by their model.
            if props.kind != Kind.BATTERY:
                name = props.model or name
            elif props.model and sum(1 for other in self.devices if other.props.kind == Kind.BATTERY) > 1:
                name = "%s (%s)" % (name, props.model)
            row.name.set_text(name)
            row.percentage.set_text("%.0f%%" % props.percentage)
            row.image.set_from_icon_name(self.icon_name(props.icon_name), Gtk.IconSize.LARGE_TOOLBAR)

    @staticmethod
    def icon_name(name):
        """Returns the name of the icon, from the theme or, if the theme has none, from xapp-symbolic-icons."""
        theme = Gtk.IconTheme.get_default()
        return next((icon for icon in ("xsi-" + name, name) if theme.has_icon(icon)), "battery-symbolic")

    def on_row_selected(self, _list, row):
        if row is None:
            return
        self.device = row.device
        self.update_details()
        self.load_history()

    def on_device_changed(self, device, _param):
        self.update_rows()
        if device is self.device:
            self.update_details()

    def update_details(self):
        props = self.device.props
        self.percentage.set_markup("<span size='xx-large' weight='bold'>%.0f%%</span>" % props.percentage)
        self.level.set_value(props.percentage)
        status = STATES.get(props.state, _("Unknown"))
        if props.state in (State.DISCHARGING, State.PENDING_DISCHARGE) and props.time_to_empty > 0:
            status = _("%(state)s, %(time)s remaining") % {"state": status, "time": format_duration(props.time_to_empty)}
        elif props.state == State.CHARGING and props.time_to_full > 0:
            status = _("%(state)s, %(time)s until full") % {"state": status, "time": format_duration(props.time_to_full)}
        self.status.set_text(status)
        self.show_row("vendor", props.vendor)
        self.show_row("model", props.model)
        self.show_row("technology", TECHNOLOGIES.get(props.technology))
        self.show_row("energy", format_number(props.energy, "Wh") if props.energy > 0 else None)
        self.show_row("energy-full", format_number(props.energy_full, "Wh") if props.energy_full > 0 else None)
        self.show_row("energy-full-design",
                      format_number(props.energy_full_design, "Wh") if props.energy_full_design > 0 else None)
        self.show_row("capacity", format_number(props.capacity, "%") if props.capacity > 0 else None)
        self.show_row("rate", format_number(props.energy_rate, "W") if props.energy_rate > 0 else None)
        self.show_row("voltage", format_number(props.voltage, "V") if props.voltage > 0 else None)
        self.show_row("charge-cycles", str(props.charge_cycles) if props.charge_cycles > 0 else None)
        self.show_row("temperature", format_number(props.temperature, "°C") if props.temperature > 0 else None)

    def show_row(self, name, text):
        """Shows a line of details with this text, or hides it if there is no text."""
        title_label, value_label = self.rows[name]
        if text:
            value_label.set_text(text)
        title_label.set_visible(bool(text))
        value_label.set_visible(bool(text))

    def on_history_choice_changed(self, _combo):
        if self.kind_combo.get_active_id() is None or self.span_combo.get_active_id() is None:
            return
        self.settings.set_string("battery-history-type", self.kind_combo.get_active_id())
        self.settings.set_string("battery-history-span", self.span_combo.get_active_id())
        self.load_history()

    def load_history(self):
        if self.device is None:
            return
        kind = self.kind_combo.get_active_id() or "charge"
        span_key = self.span_combo.get_active_id() or "3h"
        _key, seconds, divisions, _title = next(span for span in SPANS if span[0] == span_key)
        try:
            items = self.device.get_history_sync(kind, seconds, HISTORY_POINTS, None)
        except GLib.Error:
            items = None
        self.graph.set_history(items, kind, seconds, divisions)
        self.graph.set_visible(bool(items))
        self.no_history.set_visible(not items)
        self.history_loaded = GLib.get_monotonic_time()

    def refresh(self):
        """Called every second while the page is visible. The history is only reloaded from time to time."""
        if GLib.get_monotonic_time() - self.history_loaded > HISTORY_RELOAD_SECONDS * 1_000_000:
            self.load_history()
