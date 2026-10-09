#!/usr/bin/env bash
set -euo pipefail

version="${1:?Usage: build-deb.sh VERSION}"
root="$(cd -- "$(dirname -- "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT
package="$work/package"
version="${version#v}"
deb_version="${version/-/~}"
arch="$(dpkg --print-architecture)"
mkdir -p "$package/DEBIAN"
install -m 644 "$root/packaging/debian/control" "$package/DEBIAN/control"
sed -i "s/^Version:.*/Version: $deb_version/; s/^Architecture:.*/Architecture: $arch/" "$package/DEBIAN/control"
install -Dm755 "$root/packaging/reposhelf" "$package/usr/bin/reposhelf"
install -Dm644 "$root/packaging/reposhelf.desktop" "$package/usr/share/applications/reposhelf.desktop"
install -Dm644 "$root/assets/reposhelf.svg" "$package/usr/share/icons/hicolor/scalable/apps/io.github.sticklerer.RepoShelf.svg"
install -Dm644 "$root/server.py" "$package/usr/lib/reposhelf/server.py"
install -Dm644 "$root/desktop_app.py" "$package/usr/lib/reposhelf/desktop_app.py"
cp -R "$root/static" "$package/usr/lib/reposhelf/static"
cp -R "$root/assets" "$package/usr/lib/reposhelf/assets"
mkdir -p "$root/dist"
dpkg-deb --root-owner-group --build "$package" "$root/dist/reposhelf_${deb_version}_${arch}.deb"
