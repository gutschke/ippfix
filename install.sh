#!/bin/bash -e
export LC_ALL=C
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH}"

SOURCES=(ippfix{,.8,.8.md,.py,.service,.socket} ippfix-convert.socket
         ippfix-snmp.socket snmpmini.py
         'ippfix-convert@.service' ippfix.tmpfiles.conf ippcodec.py defont
         {install,uninstall}.sh LICENSE
         README.md DEPLOYMENT.md DIAGNOSING.md INTERNALS.md OPEN-QUESTIONS.md)
# The diagnostic tools are copied from ${src}/scripts below. The selftest
# scripts are deliberately not among them: they test a source tree, not an
# installation.
DEP='zeroconf'

trap 'rc="$?"
      trap "" INT TERM QUIT HUP EXIT ERR
      [ $rc -eq 0 ] || {
      tput bel
      echo
      echo "Script $0 failed unexpectedly" >&2; }
      exit $rc' INT TERM QUIT HUP EXIT ERR

[ "$(id -u)" -eq 0 ] || {
  echo 'This script must be run as "root"'
  exit 1
}

# Dependency check
missing=""
for cmd in python3 gzip mandb systemctl useradd tput gs openssl; do
  if ! command -v "$cmd" >&/dev/null; then
    missing="$missing $cmd"
  fi
done

# Check for venv (common omission on Debian/Ubuntu). Importing venv is not
# enough: Debian splits out ensurepip, without which venv creation fails only
# once we are already halfway through installing.
if ! python3 -c 'import venv, ensurepip' >&/dev/null; then
  pyver="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"
  echo 'Error: Python3 "venv" support is incomplete.'
  echo "  On Debian/Ubuntu, install it with: apt install python3${pyver:+${pyver}}-venv"
  exit 1
fi

if [ -n "$missing" ]; then
  echo "Error: Missing required system tools:$missing"
  case "$missing" in
    *gs*) echo '  Ghostscript provides "gs": apt install ghostscript';;
  esac
  exit 1
fi

script="$(readlink -f "$(type -P "$0")")"
src="${script%/*}"
U="$(tput smul)"
R="$(tput rmul)"

# Choose installation directory
cat <<EOF
${U}ippfix${R} needs to be installed in its own system directory. Common
choices are ${U}/usr/local/lib/ippfix${R} or ${U}/opt/ippfix${R}.
EOF
while :; do
  read -p 'Install path [/usr/local/lib/ippfix]: ' dst
  [ -n "${dst}" ] || dst='/usr/local/lib/ippfix'
  [[ "${dst}" =~ ^/ ]] && break || :
done

# Determine system paths
man='/usr/share/man'
if ! [[ "${dst}" =~ ^'/usr' ]]; then
  [ -d "/bin" ] && sys='/' || sys='/usr'
elif [[ "${dst}" =~ ^'/usr' ]] && ! [[ "${dst}" =~ ^'/usr/local' ]]; then
  sys='/usr'
else
  sys='/usr/local'
  man='/usr/local/share/man'
fi

# Install files
echo -n 'Copying source files...'
mkdir -m0755 -p "${dst}"
for file in "${SOURCES[@]}"; do
  [ ! -e "${src}/${file}" ] || cp "${src}/${file}" "${dst}/"
done
# Globbed against ${src} rather than the working directory, so this does the
# same thing wherever the installer is run from. The documentation tells people
# to run these tools; an install without them cannot follow its own advice.
mkdir -m0755 -p "${dst}/scripts"
for file in "${src}"/scripts/*.py; do
  [ -e "${file}" ] || continue
  cp "${file}" "${dst}/scripts/"
  chmod 0755 "${dst}/scripts/${file##*/}"
done
# fakeprinter.py reads its captured attribute reply from here, so the fixtures
# travel with it or it does not run.
mkdir -m0755 -p "${dst}/scripts/fixtures"
for file in "${src}"/scripts/fixtures/*; do
  [ -e "${file}" ] || continue
  cp "${file}" "${dst}/scripts/fixtures/"
  chmod 0644 "${dst}/scripts/fixtures/${file##*/}"
done
# Not part of the proxy, but the thing that stops --archive being forgotten.
# It works out its own path, so it does the right thing from here too.
cp "${src}/debian/pkg/ippfix-archive-reminder" "${dst}/" 2>/dev/null || :
chmod 0755 "${dst}/ippfix" "${dst}/defont"
[ ! -e "${dst}/ippfix-archive-reminder" ] || \
  chmod 0755 "${dst}/ippfix-archive-reminder"
echo ' done.'

# Setup python venv
echo -n 'Setting up Python virtual environment...'
(
  cd "${dst}"
  rm -rf 'venv'
  python3 -m 'venv' 'venv'
  ./venv/bin/pip3 install --upgrade 'pip' >&/dev/null
  ./venv/bin/pip3 install ${DEP} >/dev/null

  # Create the symlink for the process name
  # This ensures 'ps' shows 'ippfix' instead of 'python3'
  ln -sf 'python3' 'venv/bin/ippfix'
)
echo ' done.'

# System integration
echo -n 'Creating symbolic links...'
# Binary
rm -f "${sys}/bin/ippfix"
ln -s "${dst}/ippfix" "${sys}/bin/ippfix"

# Man page
rm -f "${man}/man8/ippfix.8.gz"
mkdir -p "${man}/man8"
gzip -c "${dst}/ippfix.8" >"${man}/man8/ippfix.8.gz"
echo ' done.'

echo -n 'Updating man database...'
mandb -q >&/dev/null || echo " (warning: mandb failed)"
echo ' done.'

# Service & user
echo -n 'Configuring users and storage...'
conf_dir='/etc/ippfix'
state_dir='/var/lib/ippfix'
if ! id 'ippfix' >&/dev/null; then
  useradd -d "${conf_dir}" -U -M -r -s '/usr/sbin/nologin' 'ippfix'
fi
# Ghostscript runs under its own account, separate from the network daemon, so
# that a flaw in document parsing reaches neither the keys nor the archive.
if ! id 'ippfix-convert' >&/dev/null; then
  useradd -d '/nonexistent' -U -M -r -s '/usr/sbin/nologin' 'ippfix-convert'
fi
mkdir -p "${conf_dir}" "${state_dir}"
chown 'ippfix:ippfix' "${conf_dir}" "${state_dir}"
chmod 750 "${conf_dir}"
chmod 700 "${state_dir}"
install -m0644 "${dst}/ippfix.tmpfiles.conf" '/usr/lib/tmpfiles.d/ippfix.conf'
systemd-tmpfiles --create '/usr/lib/tmpfiles.d/ippfix.conf' >&/dev/null || :
echo ' done.'

# TLS credentials. Printers ship self-signed certificates of their own, so
# clients already treat these no differently.
echo -n 'Generating TLS credentials...'
if [ -s "${conf_dir}/ippfix.key" ] && [ -s "${conf_dir}/ippfix.crt" ]; then
  echo ' kept existing.'
else
  host="$(hostname -f 2>/dev/null || hostname)"
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "${conf_dir}/ippfix.key" -out "${conf_dir}/ippfix.crt" \
    -subj "/CN=${host}" \
    -addext "subjectAltName=DNS:${host},DNS:${host%%.*}.local" \
    -addext 'extendedKeyUsage=serverAuth' >&/dev/null
  chown 'ippfix:ippfix' "${conf_dir}/ippfix.key" "${conf_dir}/ippfix.crt"
  chmod 640 "${conf_dir}/ippfix.key"
  chmod 644 "${conf_dir}/ippfix.crt"
  echo ' done.'
fi

echo -n 'Installing systemd units...'
for unit in ippfix.service ippfix.socket ippfix-convert.socket 'ippfix-convert@.service'; do
  rm -f "/etc/systemd/system/${unit}"
  # Symlink for "single source of truth" configuration
  ln -s "${dst}/${unit}" "/etc/systemd/system/${unit}"
done

# Reload
systemctl daemon-reload
systemctl stop ippfix ippfix.socket >&/dev/null || :
systemctl enable ippfix ippfix.socket ippfix-convert.socket >&/dev/null
echo ' done.'

# Warn about a conflicting mDNS responder, which is the most common reason for
# the queues never appearing on clients.
if ss -ulnH 'sport = :5353' 2>/dev/null | grep -q .; then
  cat <<EOF

${U}Note${R}: something already holds UDP port 5353. Only one mDNS responder can
publish on a host. If that is systemd-resolved, set ${U}MulticastDNS=no${R} in
/etc/systemd/resolved.conf and restart it, or run ippfix with ${U}--no-advertise${R}.
EOF
fi

# Finished
cat <<EOF

${U}ippfix${R} is now installed.

1. Edit configuration:   ${U}${dst}/ippfix.service${R}
   (Linked from /etc/systemd/system/ippfix.service)
   Set the printers on the ExecStart line, e.g.
     ${U}upstairs=ipp://printer.example/ipp/print${R}
2. Start service:        ${U}sudo systemctl start ippfix${R}
3. Check status:         ${U}sudo systemctl status ippfix${R}
4. Examine log messages: ${U}sudo journalctl -xeu ippfix${R}
5. Read manual:          ${U}man ippfix${R}
6. Uninstall:            ${U}${dst}/uninstall.sh${R}

EOF
