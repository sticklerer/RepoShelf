# RepoShelf

RepoShelf is a small local web app for browsing iOS jailbreak package repositories and keeping copies of their `.deb` packages on your computer.

> **AI-generated software:** RepoShelf was created with AI assistance. Review the code and verify downloads with sources you trust before using it; the project is provided as-is and is not affiliated with Apple or jailbreak repository providers.

## Install the desktop app (Linux)

RepoShelf opens in its own desktop window and adds a system-tray icon. Closing the window hides it; choose **Quit RepoShelf** from the tray menu to stop it.

1. Install Python 3 and virtual-environment support using your distribution's package manager:

   | Distribution | Install command |
   | --- | --- |
   | Debian, Ubuntu, Linux Mint | `sudo apt update && sudo apt install python3 python3-venv` |
   | Fedora | `sudo dnf install python3` |
   | Arch Linux, Manjaro | `sudo pacman -S python` |
   | openSUSE | `sudo zypper install python3 python3-pip` |
   | Alpine Linux | `sudo apk add python3 py3-pip` |

   The installer creates a virtual environment with `python3 -m venv`. If your distribution packages that support separately, install its Python venv package too.

2. Download the source and install the desktop launcher:

   ```sh
   git clone https://github.com/sticklerer/RepoShelf.git
   cd RepoShelf
   ./install-linux.sh
   ```

The installer creates a private Python environment, installs the packages listed in [`requirements.txt`](requirements.txt), and adds RepoShelf to your desktop Applications menu. Launch it from there. A working desktop system tray is required. The installer does not enable automatic startup when you log in.

The installed app and its downloaded packages are kept under `~/.local/share/reposhelf/` (or `$XDG_DATA_HOME/reposhelf/`). Re-running the installer to update the app preserves your saved settings and downloads.

### Uninstall

Use **Uninstall RepoShelf…** in the desktop window's toolbar or system-tray menu and choose whether to keep or delete saved settings and downloads. From a terminal, run the installed uninstaller:

```sh
~/.local/share/reposhelf/uninstall-linux.sh
```

The command removes the app and desktop launcher but keeps settings and downloaded packages. Add `--purge-data` to delete those too. If you cloned the repository, you can also run `./uninstall-linux.sh`; this removes the installed app, not the source checkout.

## Run the web server directly (optional)

Requires Python 3.10 or newer. The local web server itself uses only the standard library; installing `keyring` is required to use saved credentials or Sileo-compatible device profiles. This mode runs in a terminal, without the desktop window or tray.

```sh
python3 -m pip install keyring
python3 server.py
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). Stop the server with `Ctrl+C`. By default, it listens only on your computer. To use it from another device on your trusted local network, start it with `python3 server.py --host 0.0.0.0` and open the computer's LAN address on that device.

## Use it

- Add a repository using its base URL. RepoShelf checks common flat `Packages` indexes (plain, gzip, bzip2, or xz) and Debian-style iOS indexes.
- Browse and search the package list, select individual packages, or use **Get all** to download every package in the current view.
- Downloaded `.deb` files are stored in `data/downloads/`; repo settings and package indexes are stored in `data/state.json`.
- **Automation** checks sources on the chosen schedule and downloads packages that are new or have a version not already saved. It works only while RepoShelf is running. **Run now** starts a check immediately.
- RepoShelf spaces out requests to each repository host, caches repository indexes, and honors `Retry-After` with bounded retries for temporary server errors. Requests identify themselves as RepoShelf; use only repository-supported credentials and device IDs authorized for your account.
- Repository sources must use HTTPS so package indexes and downloads cannot be silently replaced in transit. Existing HTTP sources must be removed and re-added with their HTTPS URL.
- For private APT repositories, **Add a source** supports HTTP Basic credentials or a provider-issued API token using standard bearer-token authorization. Credentials are kept in your operating system's keyring, not in `data/state.json`; the keyring must be unlocked while RepoShelf accesses the source.
- New sources default to a Sileo-compatible request profile with editable model, iOS version, architecture, and client version. RepoShelf generates one random 40-character archive ID, stores it in the OS keyring, and sends it as `X-Unique-ID` along with the documented `X-Machine`, `X-Firmware`, and `Sec-CH-UA-*` headers. This ID is local to RepoShelf; it is not read from or tied to a physical device. For Havoc, Chariz, and YouRepo hostnames, automatic IDs are disabled: enter the device ID that the provider has authorized for your account. Other paid/private repos can be switched to manual-ID mode in the add-source form. Device IDs and credentials stay in the OS keyring and are sent only over HTTPS. See [Sileo's request-header implementation](https://github.com/Sileo/Sileo/blob/master/Sileo/Backend/URL%20Manager/URLManager.swift) and [device-header definitions](https://github.com/Sileo/Sileo/blob/master/Sileo/Backend/Extensions/UIDevice%2BPrivate.swift).
- This is not a provider-specific Havoc, Chariz, or YouRepo web-login integration: website OAuth, purchase-account APIs, device registration, and custom repository protocols are not guessed or emulated. Use an official token or repository credential only if the provider documents it for the APT repository endpoint. Provider-specific sign-in needs a documented, supported API from that provider.
- Remove a source with the **×** beside its name in the sidebar. Removing a source does not delete downloaded files.

RepoShelf verifies downloaded files against repository-provided size and SHA256 metadata when available. It does not install packages, verify repository signatures, or run package contents.

## Run tests

Run the local server and package-download tests with Python's standard library test runner:

```sh
python3 -m unittest discover -s tests -v
```
