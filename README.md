# RepoShelf

RepoShelf is a small local web app for browsing iOS jailbreak package repositories and keeping copies of their `.deb` packages on your computer.

> **AI-generated software:** RepoShelf was created with AI assistance. Review the code and verify downloads with sources you trust before using it; the project is provided as-is and is not affiliated with Apple or jailbreak repository providers.

![alt text](https://github.com/sticklerer/RepoShelf/blob/main/showcase.png?raw=true)

> **Image disclaimer:** The repos shown on the showcase image were used for demonstration only! All the downloaded content was purged after.
>  **Don't use RepoShelf for Piracy!**

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

### Install a beta package

The [GitHub Releases page](https://github.com/sticklerer/RepoShelf/releases) provides native Linux packages for Debian/Ubuntu (`.deb`), Fedora (`.rpm`), and Arch Linux (`.pkg.tar.zst`), plus a Flatpak bundle. Beta releases are experimental and may contain bugs; RepoShelf is AI-generated software, so review its behavior and only download packages from sources you trust. These downloaded packages are not an APT/DNF repository; download the matching release asset first, then install it locally:

| System | Install the downloaded release file |
| --- | --- |
| Debian, Ubuntu, Linux Mint | `sudo apt install ./reposhelf_*_amd64.deb` |
| Fedora | `sudo dnf install ./reposhelf-*.rpm` |
| Arch Linux | `sudo pacman -U ./reposhelf-*-any.pkg.tar.zst` |
| Arch Linux with yay | `yay -U ./reposhelf-*-any.pkg.tar.zst` |
| Flatpak | `flatpak install --user --bundle ./RepoShelf.flatpak io.github.sticklerer.RepoShelf` |

Flatpak requires the Flathub remote for the Freedesktop 24.08 runtime. The Arch release also includes `reposhelf-aur.tar.gz` with a hash-pinned `PKGBUILD` and `.SRCINFO`; extract it and run `makepkg -si` to build and install from source. `yay` uses the same Arch package and PKGBUILD; it is not a separate package format or an AUR listing.

Package-manager installations keep per-user settings and downloads in `~/.local/share/reposhelf/`; remove the application using the same package manager. The source installer remains available as an alternative and updates its own installation.

The installed app and its downloaded packages are kept under `~/.local/share/reposhelf/` (or `$XDG_DATA_HOME/reposhelf/`). Re-running the installer to update the app preserves your saved settings and downloads. Configure automatic checks from the **Automation** tab with a shared repeat interval, nested-group intervals, and optional per-source overrides, using seconds, minutes, hours, days, or years (up to 99 years). Group intervals inherit through nested groups; a source-specific interval takes precedence. In **Settings**, you can disable grouping without losing group assignments, turn off the automation setup banner, keep the movable download status visible until active downloads finish (including across brief connection interruptions or a view reload), hide device information when adding repositories (paid sources still show their required authorized-ID fields), and enable diagnostic logging to `data/reposhelf.log`; logging is off by default.

When removing a repository, RepoShelf asks whether to also delete its downloaded package files. Keeping them is the default; deletion is limited to package files recorded under RepoShelf's downloads directory.

### Uninstall

For the source installation, open **Settings** in the app sidebar and choose **Uninstall RepoShelf…**, or run the installed uninstaller:

```sh
~/.local/share/reposhelf/uninstall-linux.sh
```

The command removes the app and desktop launcher but keeps settings and downloaded packages. Add `--purge-data` to delete those too. If you cloned the repository, you can also run `./uninstall-linux.sh`; this removes the installed app, not the source checkout. For a package-manager installation, use that manager's normal remove command (for example, `sudo apt remove reposhelf`, `sudo dnf remove reposhelf`, `sudo pacman -R reposhelf`, or `flatpak uninstall io.github.sticklerer.RepoShelf`). Per-user downloaded packages and settings are kept.

## Run the web server directly (optional)

Requires Python 3.10 or newer. The local web server itself uses only the standard library; installing `keyring` is required to use saved credentials or Sileo-compatible device profiles. This mode runs in a terminal, without the desktop window or tray.

```sh
python3 -m pip install keyring
python3 server.py
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). Stop the server with `Ctrl+C`. By default, it listens only on your computer. To use it from another device on your trusted local network, start it with `python3 server.py --host 0.0.0.0` and open the computer's LAN address on that device.

## Use it

- Add a repository using its base URL. RepoShelf checks common flat `Packages` indexes (plain, gzip, bzip2, or xz) and Debian-style iOS indexes.
- Browse and search the package list, select individual packages, or use **Get all** to download every matching package across all pages. Bulk selection is resolved locally in linear time, so large repositories do not need to send huge ID lists from the browser or freeze the interface while preparing the queue.
- Create nested source groups from the sidebar’s group manager, assign sources to them, and browse or download packages from a group or several selected groups. **All packages** continues to show every package until you choose a source or group. The download queue can also be filtered to jobs containing selected groups.
- Track download percentages, package counts, byte totals, and transfer rates. The **Download queue** lets you pause, resume, or cancel a job, remove packages that have not started, inspect failed package reasons, and retry individual failures or all failures; large queues are paginated.
- Package names use the repository's `Name` field when available, with the package/bundle ID shown beneath. Repository display names use `Origin`/`Label`; source and tweak icons load from common Cydia icon paths or package `Icon` URLs when available. Missing icons use the fallback symbol.
- Downloaded `.deb` files are stored in `data/downloads/`; repo settings and package indexes are stored in `data/state.json`.
- **Automation** checks sources on the chosen schedule and downloads packages that are new or have a version not already saved. Group intervals are inherited by nested groups and their sources, and a source-specific interval overrides its group. It works only while RepoShelf is running. **Run now** starts a check immediately.
- RepoShelf spaces out requests to each repository host, caches repository indexes, and honors `Retry-After` with bounded retries for temporary server errors. Requests identify themselves as RepoShelf; use only repository-supported credentials and device IDs authorized for your account.
- HTTP and HTTPS repository sources are supported. HTTPS is strongly preferred because HTTP does not protect package indexes or downloads from interception or modification. HTTP sources are limited to public repositories: RepoShelf will not send credentials or device details over an unencrypted connection. Use HTTPS for private or device-authorized sources.
- Repository hosts must resolve exclusively to public IP addresses, and redirects to another origin are blocked to prevent a repository URL from reaching local or private-network services.
- For private APT repositories, **Add a source** supports HTTP Basic credentials or a provider-issued API token using standard bearer-token authorization. Credentials are kept in your operating system's keyring, not in `data/state.json`; the keyring must be unlocked while RepoShelf accesses the source.
- New HTTPS sources default to a Sileo-compatible request profile with editable model, iOS version, architecture, and client version. In **Add a source**, select **Generate compatible device details** to fill these fields from a supported profile and generate a new random 40-character archive ID. RepoShelf stores the ID in the OS keyring and sends it as `X-Unique-ID` along with the documented `X-Machine`, `X-Firmware`, and `Sec-CH-UA-*` headers. This ID is local to RepoShelf; it is not read from or tied to a physical device. For Havoc, Chariz, and YouRepo hostnames, automatic IDs are disabled: enter the device ID that the provider has authorized for your account. Other paid/private repos can be switched to manual-ID mode in the add-source form. Device IDs and credentials stay in the OS keyring and are sent only over HTTPS. HTTP sources never receive credentials or device-identifying headers. See [Sileo's request-header implementation](https://github.com/Sileo/Sileo/blob/master/Sileo/Backend/URL%20Manager/URLManager.swift) and [device-header definitions](https://github.com/Sileo/Sileo/blob/master/Sileo/Backend/Extensions/UIDevice%2BPrivate.swift).
- This is not a provider-specific Havoc, Chariz, or YouRepo web-login integration: website OAuth, purchase-account APIs, device registration, and custom repository protocols are not guessed or emulated. Use an official token or repository credential only if the provider documents it for the APT repository endpoint. Provider-specific sign-in needs a documented, supported API from that provider.
- Remove a source with the **×** beside its name in the sidebar. RepoShelf asks whether to also delete downloaded files; keeping them is the default.

RepoShelf verifies downloaded files against repository-provided size and SHA256 metadata when available. It does not install packages, verify repository signatures, or run package contents.

## Run tests

Run the local server and package-download tests with Python's standard library test runner:

```sh
python3 -m unittest discover -s tests -v
```
