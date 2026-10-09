import gettext
import locale

from gi.repository import GLib
from xapp.util import l10n

_ = l10n("superlight")

# pixels between page content and edges, and between tables and their button box
MARGIN = 10


def format_decimal(value):
    """One decimal between 0 and 10, none otherwise."""
    if round(value, 1) >= 10:
        return "%d" % round(value)
    return locale.format_string("%.1f", round(value, 1))

def format_size(size):
    """One decimal between 0 and 10, none otherwise."""
    if size < 1000:
        return GLib.format_size(size)
    value = float(size)
    units = ("kB", "MB", "GB", "TB", "PB")
    for unit in units:
        value /= 1000
        if round(value, 1) < 1000 or unit == units[-1]:
            break
    return "%s\u00a0%s" % (format_decimal(value), unit)

def format_value(kind, value):
    match kind:
        case "text":
            return value
        case "number":
            return str(value)
        case "percent":
            return format_decimal(value) + "%"
        case "size":
            return format_size(value)
        case "gpu":
            if value >= 0.05:
                return format_decimal(value) + "%"
            return ""
        case "rate":
            if value <= 0:
                return ""
            return _("%s/s") % format_size(value)
        case "total":
            if value <= 0:
                return ""
            return format_size(value)
        case "priority":
            if value < -7:
                # Translators: Priority
                return _("Very High")
            if value < -2:
                # Translators: Priority
                return _("High")
            if value < 3:
                # Translators: Priority
                return _("Normal")
            if value < 7:
                # Translators: Priority
                return _("Low")
            # Translators: Priority
            return _("Very Low")
        case "duration":
            hours, remainder = divmod(int(value), 3600)
            return "%d:%02d:%02d" % (hours, *divmod(remainder, 60))
        case "time":
            started = GLib.DateTime.new_from_unix_local(value)
            today = GLib.DateTime.new_now_local()
            day = started.format("%F")
            if day == today.format("%F"):
                return _("Today, %s") % started.format("%H:%M")
            if day == today.add_days(-1).format("%F"):
                return _("Yesterday, %s") % started.format("%H:%M")
            return started.format("%x %H:%M")
        case "state":
            states = {
                # Translators: Process
                "R": _("Running"),
                # Translators: Process
                "S": _("Sleeping"),
                "D": _("Disk Sleep"),
                # Translators: Process
                "T": _("Stopped"),
                "t": _("Tracing Stop"),
                "Z": _("Zombie"),
                # Translators: Process
                "X": _("Dead"),
                # Translators: Process
                "I": gettext.pgettext("process state", "Idle"),
            }
            return states.get(value, value)


def rgb(color):
    """Converts HEX string #RRGGBB into what (r,g,b), each from 0 to 1, as cairo wants them."""
    red = int(color[1:3], 16)
    green = int(color[3:5], 16)
    blue = int(color[5:7], 16)
    return (round(red / 255, 3), round(green / 255, 3), round(blue / 255, 3))
