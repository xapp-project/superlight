# Superlight 🦸💡

System monitor for Linux desktops.

## Philosophy

- Intuitive
- Just enough configuration to feel at home
- Simple and easy to use
- Lets you quickly see what's taking resources on the computer

## Pages

- Resources
- Apps
- Processes
- Systemd
- Battery (only shows if you have a battery)
- Storage
- Logs (not implemented yet)

## Notes

In the storage page, "Trash" represents the local trash, i.e. only what's stored on the drive you selected. When you click "Empty trash", it only empties the local trash. Many file manager, such as Nemo, use a global trash://, which combines the trashed files from all the different drives.

## Building from source

### For Mint with mint-dev-tools

```bash
# Install mint-dev-tools
apt install mint-dev-tools
# Remove any previous versions
apt remove 'superlight*'
# Build and install from github
mint-build -i -g https://github.com/xapp-project/superlight.git
```

### For Debian distributions (Mint, Ubuntu, etc.) with dpkg-buildpackage

```bash
# Get the source code..
git clone https://github.com/xapp-project/superlight.git
# Go in..
cd superlight
# Install the build dependencies..
sudo apt build-dep --mark-auto .
# Remove any previously built packages
rm -f ../superlight*.deb
# Build
dpkg-buildpackage
# Install
sudo apt install ../superlight*.deb
```

### For other distributions with meson

```bash
# Get the source code..
git clone https://github.com/xapp-project/superlight.git
# Go in..
cd superlight
```

Install the build and runtime dependencies for your distribution. For example:

```bash
# Fedora: sudo dnf install meson ninja-build python3 gettext
# Arch: sudo pacman -S meson ninja python gettext
# openSUSE: sudo zypper install meson ninja python3 gettext-tools
```

The dependencies are listed below (using debian pkg names, names may be different in your distribution). Install all of them.

#### Dependencies

```text
gir1.2-flatpak-1.0 (to list what is installed with Flatpak, optional)
gir1.2-gtk-3.0
gir1.2-upowerglib-1.0
gir1.2-xapp-1.0
python3-cairo
python3-gi
python3-gi-cairo
python3-setproctitle
python3-xapp
upower
```

#### Dependencies for building

```text
gettext
libglib2.0-dev or libgio-2.0-dev
meson
pkg-config
```

#### Dependencies for runtime

```text
xapp-symbolic-icons
```

#### Build and install

```bash
meson setup build --prefix=/usr/local
meson compile -C build
sudo meson install -C build
```

#### Uninstall

To remove a Meson installation while retaining the build directory:

```bash
sudo ninja -C build uninstall
```

## Translations

Please use Launchpad to translate this project: https://translations.launchpad.net/linuxmint/latest/.

The PO files in this project are imported from there.

## License

Code: GPLv3+
