import signal
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("XApp", "1.0")
from gi.repository import Gio, GLib, Gtk, XApp
from setproctitle import setproctitle

from superlight import _
from superlight.config import VERSION
from superlight.common_process import ProcessScanner
from superlight.page_resources import ResourcesPage
from superlight.page_battery import BatteryPage, PowerMonitor
from superlight.page_apps import AppsPage, Catalog, group_apps
from superlight.page_processes import ProcessesPage
from superlight.page_storage import StoragePage

PAGES = ("resources", "apps", "processes", "battery", "storage")

class SuperlightWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=_("System Monitor"))
        self.settings = app.settings
        self.set_default_size(850, 680)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_icon_name("superlight")
        self.resources = ResourcesPage(self.settings)
        self.scanner = ProcessScanner()
        self.processes = ProcessesPage(self, self.settings, self.scanner)
        self.battery_page = BatteryPage(self.settings)
        self.storage_page = StoragePage(self)
        self.catalog = Catalog()
        self.apps_page = AppsPage(self, self.settings, self.scanner, self.catalog)

        header = Gtk.HeaderBar(title=_("System Monitor"), show_close_button=True)
        menu = Gtk.Menu()
        menu.append(Gtk.MenuItem(label=_("About"), action_name="app.about"))
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(Gtk.MenuItem(label=_("Quit"), action_name="app.quit"))
        menu.show_all()
        menu_button = Gtk.MenuButton(popup=menu)
        menu_button.set_image(Gtk.Image.new_from_icon_name("xsi-open-menu-symbolic", Gtk.IconSize.BUTTON))
        header.pack_end(menu_button)
        self.set_titlebar(header)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_UP_DOWN)
        self.stack.add_titled(self.resources, "resources", _("Resources"))
        if group_apps(self.scanner.scan_now({"unit", "command"}), GLib.get_user_name(), self.catalog):
            self.stack.add_titled(self.apps_page, "apps", _("Apps"))
            self.stack.child_set_property(self.apps_page, "icon-name", "xsi-apps-symbolic")
        self.stack.add_titled(self.processes, "processes", _("Processes"))
        self.stack.add_titled(self.storage_page, "storage", _("Storage"))
        self.stack.child_set_property(self.storage_page, "icon-name", "xsi-drive-harddisk-symbolic")
        self.stack.child_set_property(self.resources, "icon-name", "xsi-cpu-symbolic")
        self.stack.child_set_property(self.processes, "icon-name", "xsi-executable-symbolic")

        sidebar = XApp.StackSidebar()
        sidebar.set_stack(self.stack)
        sidebar.set_size_request(180, -1)
        sidebar.get_style_context().add_class("sidebar")

        layout = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        layout.pack_start(sidebar, False, False, 0)
        layout.pack_start(self.stack, True, True, 0)
        self.add(layout)
        layout.show_all()
        self.power = PowerMonitor(self.update_battery_page)
        self.update_battery_page()
        self.show_page(self.settings.get_string("page"))
        self.stack.connect("notify::visible-child", self.on_page_changed)
        self.on_page_changed()

        self.sample_timer = GLib.timeout_add(1000, self.refresh)
        self.connect("destroy", self.stop_timers)

    def show_page(self, name):
        if name in PAGES and self.stack.get_child_by_name(name) is not None:
            self.stack.set_visible_child_name(name)

    def update_battery_page(self):
        devices = self.power.devices()
        present = self.battery_page.get_parent() is not None
        if devices and not present:
            self.stack.add_titled(self.battery_page, "battery", _("Battery"))
            self.stack.child_set_property(self.battery_page, "icon-name", "xsi-battery-level-100-symbolic")
            self.stack.child_set_property(self.battery_page, "position",
                                          self.stack.child_get_property(self.storage_page, "position"))
            self.battery_page.show_all()
        elif not devices and present:
            if self.stack.get_visible_child() is self.battery_page:
                self.show_page("resources")
            self.stack.remove(self.battery_page)
        if devices:
            self.battery_page.set_devices(devices)

    def on_page_changed(self, *_):
        name = self.stack.get_visible_child_name()
        if name is None:
            return
        self.settings.set_string("page", name)
        if name == "processes":
            self.processes.refresh()
        if name == "apps":
            self.apps_page.refresh()
        if name == "storage":
            self.storage_page.refresh()
        if name == "battery":
            self.battery_page.load_history()

    def stop_timers(self, _window):
        GLib.source_remove(self.sample_timer)
        self.storage_page.stop()
        self.resources.stop()

    def refresh(self):
        self.resources.refresh()
        if self.stack.get_visible_child() is self.processes:
            self.processes.refresh()
        elif self.stack.get_visible_child() is self.apps_page:
            self.apps_page.refresh()
        elif self.stack.get_visible_child() is self.battery_page:
            self.battery_page.refresh()
        return True


class SuperlightApplication(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.x.superlight", flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.settings = Gio.Settings.new("org.x.superlight")
        self.window = None
        self.add_main_option("page", 0, GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
                             "Show a page: %s" % ", ".join(PAGES), "PAGE")

    def do_startup(self):
        Gtk.Application.do_startup(self)
        for name, handler in {
            "about": lambda *_: self.show_about(),
            "quit": lambda *_: self.quit(),
        }.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)
        self.set_accels_for_action("app.quit", ["<Ctrl>q"])

    def do_activate(self):
        if not self.window:
            self.window = SuperlightWindow(self)
        self.window.show_all()
        self.window.present()

    def do_command_line(self, cmdline):
        options = cmdline.get_options_dict().end().unpack()
        page = options.get("page")
        if page is not None and page not in PAGES:
            cmdline.printerr_literal("Unknown page: %s\n" % page)
            return 1
        self.activate()
        if page is not None:
            self.window.show_page(page)
        return 0

    def show_about(self):
        about = Gtk.AboutDialog(transient_for=self.window, modal=True, program_name="Superlight",
                                version=VERSION, comments=_("System Monitor"),
                                logo_icon_name="superlight", license_type=Gtk.License.GPL_3_0,
                                website="https://github.com/xapp-project/superlight")
        about.connect("response", lambda dialog, _response: dialog.destroy())
        about.present()


def main(argv):
    setproctitle("superlight")
    GLib.set_prgname("org.x.superlight")
    GLib.set_application_name(_("System Monitor"))
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    return SuperlightApplication().run(argv)
