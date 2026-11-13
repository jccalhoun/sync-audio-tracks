# PyInstaller recipe: one folder holding micsync-gui.exe (the window) and
# micsync.exe (command line), sharing a single copy of Python and the libraries.
# A folder starts much faster than a single-file exe, which unpacks itself on
# every launch.  Build with:  pyinstaller --noconfirm --clean micsync.spec
from PyInstaller.utils.hooks import collect_all

dnd_datas, dnd_binaries, dnd_hidden = collect_all("tkinterdnd2")

gui = Analysis(["micsync_gui.pyw"], datas=dnd_datas, binaries=dnd_binaries, hiddenimports=dnd_hidden + ["micsync"])
cli = Analysis(["micsync.py"])

gui_exe = EXE(PYZ(gui.pure), gui.scripts, [], exclude_binaries=True, name="micsync-gui", console=False)
cli_exe = EXE(PYZ(cli.pure), cli.scripts, [], exclude_binaries=True, name="micsync", console=True)

COLLECT(gui_exe, cli_exe, gui.binaries, gui.datas, cli.binaries, cli.datas, name="micsync")
