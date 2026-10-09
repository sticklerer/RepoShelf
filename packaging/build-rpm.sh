#!/usr/bin/env bash
set -euo pipefail

version="${1:?Usage: build-rpm.sh VERSION}"
root="$(cd -- "$(dirname -- "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT
version="${version#v}"
rpm_version="${version/-/~}"
mkdir -p "$work"/{BUILD,RPMS,SOURCES,SPECS,SRPMS}
git -C "$root" archive --format=tar --prefix=RepoShelf/ HEAD | gzip -n > "$work/SOURCES/reposhelf-source.tar.gz"
rpmbuild -bb \
  --define "_topdir $work" \
  --define "repo_version $rpm_version" \
  "$root/packaging/rpm/reposhelf.spec"
mkdir -p "$root/dist"
find "$work/RPMS" -type f -name '*.rpm' -exec install -m 644 {} "$root/dist/reposhelf-${rpm_version}-1.$(rpm --eval '%{_arch}').rpm" \;
