project('superlight', version : '1.0.0', meson_version : '>= 0.59.0')

prefix = get_option('prefix')
datadir = get_option('datadir')
bindir = get_option('bindir')

python3 = import('python').find_installation('/usr/bin/python3')
python_module_dir = python3.get_install_dir()

# Debian uses different Python layouts for /usr and /usr/local. Meson uses
# the packaged-system layout, even with a /usr/local prefix. For Python's
# default installation prefix, use its default module directory instead.
python_default_paths = run_command(
  python3, '-c',
  'import sysconfig; print(sysconfig.get_path("data")); print(sysconfig.get_path("purelib"))',
  check: true,
).stdout().strip().split('\n')
python_default_prefix = python_default_paths[0]
python_default_module_dir = python_default_paths[1]
if prefix == python_default_prefix
  python_module_dir = python_default_module_dir
endif
python_module_dir = join_paths(python_module_dir, 'superlight')

install_data(
  'superlight/__init__.py',
  'superlight/application.py',
  'superlight/common_cgroup.py',
  'superlight/common_disk.py',
  'superlight/common_graphs.py',
  'superlight/common_proc.py',
  'superlight/common_process.py',
  'superlight/page_apps.py',
  'superlight/page_battery.py',
  'superlight/page_processes.py',
  'superlight/page_resources.py',
  'superlight/page_storage.py',
  'superlight/page_systemd.py',
  'superlight/view_folders.py',
  install_dir: python_module_dir,
)

install_data(
  'superlight/chunks/__init__.py',
  'superlight/chunks/cache.py',
  'superlight/chunks/chunk.py',
  'superlight/chunks/flatpak.py',
  'superlight/chunks/folder.py',
  'superlight/chunks/hidden_files.py',
  'superlight/chunks/logs.py',
  'superlight/chunks/other.py',
  'superlight/chunks/personal_files.py',
  'superlight/chunks/snapshots.py',
  'superlight/chunks/steam.py',
  'superlight/chunks/swap.py',
  'superlight/chunks/system_cache.py',
  'superlight/chunks/temp_files.py',
  'superlight/chunks/trash.py',
  install_dir: join_paths(python_module_dir, 'chunks'),
)

configure_file(
  input: 'superlight/config.py.in',
  output: 'config.py',
  configuration: {
    'version': meson.project_version(),
  },
  install: true,
  install_dir: python_module_dir,
)

install_data(
  'data/superlight',
  install_dir: bindir,
)

subdir('data')
subdir('po')
