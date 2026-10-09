Name:           reposhelf
Version:        %{repo_version}
Release:        1%{?dist}
Summary:        Local desktop package manager for jailbreak repositories
License:        Unknown (no license declared)
BuildArch:      noarch
Requires:       python3
Requires:       python3-qt6
Requires:       python3-qt6-webengine
Requires:       python3-keyring
Source0:        reposhelf-source.tar.gz

%description
RepoShelf browses iOS jailbreak package sources and downloads packages to a
local library. This AI-generated software is an early beta release.

%prep
%setup -q -n RepoShelf

%install
install -Dpm0755 packaging/reposhelf %{buildroot}%{_bindir}/reposhelf
install -Dpm0644 packaging/reposhelf.desktop %{buildroot}%{_datadir}/applications/reposhelf.desktop
install -Dpm0644 assets/reposhelf.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/io.github.sticklerer.RepoShelf.svg
install -Dpm0644 server.py %{buildroot}%{_prefix}/lib/reposhelf/server.py
install -Dpm0644 desktop_app.py %{buildroot}%{_prefix}/lib/reposhelf/desktop_app.py
cp -a static %{buildroot}%{_prefix}/lib/reposhelf/static
cp -a assets %{buildroot}%{_prefix}/lib/reposhelf/assets

%files
%{_bindir}/reposhelf
%{_datadir}/applications/reposhelf.desktop
%{_datadir}/icons/hicolor/scalable/apps/io.github.sticklerer.RepoShelf.svg
%{_prefix}/lib/reposhelf

%changelog
* Thu Oct 09 2026 RepoShelf contributors <reposhelf@users.noreply.github.com> - 0.1.0
- Initial beta package
