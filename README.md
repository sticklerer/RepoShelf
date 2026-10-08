# RepoShelf

RepoShelf is a small local web app for browsing iOS jailbreak package repositories and keeping copies of their `.deb` packages on your computer.

## Run it

Requires Python 3.10 or newer; no third-party Python packages are needed.

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
