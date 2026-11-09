Name:           barclaw
Version:        0.1.0
Release:        1%{?dist}
Summary:        Talk to your OpenClaw agent from the Linux menu bar
License:        GPL-3.0-or-later
URL:            https://github.com/sjcsoftware/barclaw
Source0:        %{url}/releases/download/v%{version}/barclaw-%{version}.tar.gz
BuildArch:      noarch
BuildRequires:  python3-devel
Requires:       python3 >= 3.10, python3-gobject, python3-cairo, python3-cryptography, gtk3, libsoup3
Recommends:     tesseract, google-noto-color-emoji-fonts, xprop

%description
A menu bar app for an OpenClaw gateway: hold a key to speak, watch replies
stream in as Markdown, math, charts, and images, and let your agent read your
screen on request.

%prep
%autosetup

%install
install -d %{buildroot}%{python3_sitelib}
cp -r barclaw %{buildroot}%{python3_sitelib}/
install -d %{buildroot}%{_bindir}
printf '#!/usr/bin/python3\nimport sys\nfrom barclaw.application import main\nsys.exit(main())\n' > %{buildroot}%{_bindir}/barclaw
chmod 755 %{buildroot}%{_bindir}/barclaw
install -Dm644 dev.barclaw.Barclaw.desktop %{buildroot}%{_datadir}/applications/dev.barclaw.Barclaw.desktop
install -Dm644 icon.png %{buildroot}%{_datadir}/icons/hicolor/256x256/apps/dev.barclaw.Barclaw.png

%files
%{python3_sitelib}/barclaw/
%{_bindir}/barclaw
%{_datadir}/applications/dev.barclaw.Barclaw.desktop
%{_datadir}/icons/hicolor/256x256/apps/dev.barclaw.Barclaw.png
