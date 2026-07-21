# Windowless Native Messaging Host on Windows

Chrome launches the executable stored in the Native Messaging manifest directly. Process flags used by Control Center therefore cannot hide a console created by that browser-owned executable.

The supported candidate is built from `packaging/windows/native_host_entry.py` with PyInstaller `--windowed --onedir`. The GUI-subsystem executable does not allocate a console. Because a windowed PyInstaller process may not expose ordinary Python stdin and stdout objects, `bdb_bridge.windows_stdio.resolve_native_binary_stdio()` reopens Chrome's inherited standard pipe handles through `GetStdHandle` and `msvcrt.open_osfhandle`.

This keeps the existing framed Native Messaging protocol. It does not allocate a console, open a network listener, or introduce another transport.

Build the artifact with `scripts/Build-BDBNativeHostWindowless.ps1`. Install it with `scripts/Install-BDBNativeHost.ps1` and the `RequireWindowless` switch.

The dedicated Windows acceptance verifies both properties on the same executable: its PE subsystem is Windows GUI (2), and a real framed status request succeeds through redirected binary stdin and stdout. A header-only patch or a pythonw launcher without inherited pipe restoration does not satisfy this gate.
