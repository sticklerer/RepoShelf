# RepoShelf

RepoShelf is a small local web app for browsing iOS jailbreak package repositories and keeping copies of their `.deb` packages on your computer.

## Install the desktop app (Linux)

RepoShelf opens in its own desktop window and adds a system-tray icon. Closing the window hides it; choose **Quit RepoShelf** from the tray menu to stop it.

1. Install Python 3 and its virtual-environment support. On Debian or Ubuntu:

   ```sh
   sudo apt update
   sudo apt install python3 python3-venv
   ```

2. Download the source and install the desktop launcher:

   ```sh
   git clone https://github.com/sticklerer/RepoShelf.git
   cd RepoShelf
   ./install-linux.sh
   ```

The installer creates a private Python environment, installs the packages listed in [`requirements.txt`](requirements.txt), and adds RepoShelf to your desktop Applications menu. Launch it from there. A working desktop system tray is required. The installer does not enable automatic startup when you log in.

The installed app and its downloaded packages are kept under `~/.local/share/reposhelf/` (or `$XDG_DATA_HOME/reposhelf/`). Re-running the installer to update the app preserves your saved settings and downloads.

## Run the web server directly (optional)

Requires Python 3.10 or newer and no third-party packages. This mode runs in a terminal, without the desktop window or tray.

```sh
python3 server.py
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). Stop the server with `Ctrl+C`. By default, it listens only on your computer. To use it from another device on your trusted local network, start it with `python3 server.py --host 0.0.0.0` and open the computer's LAN address on that device.

## Use it

- Add a repository using its base URL. RepoShelf checks common flat `Packages` indexes (plain, gzip, bzip2, or xz) and Debian-style iOS indexes.
- Browse and search the package list, select individual packages, or use **Get all** to download every package in the current view.
- Downloaded `.deb` files are stored in `data/downloads/`; repo settings and package indexes are stored in `data/state.json`.
- **Automation** checks sources on the chosen schedule and downloads packages that are new or have a version not already saved. It works only while RepoShelf is running. **Run now** starts a check immediately.
- Remove a source with the **×** beside its name in the sidebar. Removing a source does not delete downloaded files.

RepoShelf verifies downloaded files against repository-provided size and SHA256 metadata when available. It does not install packages, verify repository signatures, or run package contents.
