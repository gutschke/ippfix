#!/bin/bash -e
export LC_ALL='C'
export PATH='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'

trap 'rc="$?"
      trap "" INT TERM QUIT HUP EXIT ERR
      [ "${rc}" -eq 0 ] || {
      tput bel
      echo
      echo "Script ${0} failed unexpectedly" >&2; }
      exit "${rc}"' INT TERM QUIT HUP EXIT ERR

[ "$(id -u)" -eq 0 ] || {
  echo 'This script must be run as "root"'
  exit 1
}

# Stop and disable service
echo -n 'Disabling systemd daemon...'

# Try to detect installation path before we kill the unit info
service_path=''
if systemctl cat 'ippfix' >&/dev/null; then
  exe="$(systemctl cat ippfix |
         sed 's/^\s*ExecStart\s*=\s*\(\S\+\).*/\1/i;t1;d;:1;q')"
  candidate="${exe%/*}"
  if [ -f "${candidate}/ippfix.py" ]; then
    service_path="${candidate}"
  else
    # For "venv" environments, need to go up two more directories
    candidate="${candidate%/*/*}"
    [ ! -f "${candidate}/ippfix.py" ] || service_path="${candidate}"
  fi
fi

for unit in ippfix.service ippfix.socket ippfix-convert.socket 'ippfix-convert@.service'; do
  systemctl stop "${unit}" >&/dev/null || :
  systemctl disable "${unit}" >&/dev/null || :
  rm -f "/etc/systemd/system/${unit}"
done
rm -f '/usr/lib/tmpfiles.d/ippfix.conf'
systemctl daemon-reload
echo ' done.'

# Determine paths to clean
dst=''
if [ -n "${service_path}" ]; then
  dst="${service_path}"
elif command -v 'ippfix' >&/dev/null; then
  # Fallback: resolve symlink /usr/local/bin/ippfix -> /opt/ippfix/ippfix
  real_path="$(readlink -f "$(command -v ippfix)")"
  candidate="${real_path%/*}"
  [ ! -f "${candidate}/ippfix.py" ] || dst="${candidate}"
fi

# Clean symbolic links & man pages
echo -n 'Removing system links...'
rm -f '/usr/local/bin/ippfix' '/usr/bin/ippfix' '/bin/ippfix'
rm -f '/usr/local/share/man/man8/ippfix.8'* \
      '/usr/share/man/man8/ippfix.8'*
echo ' done.'

# Remove main directory
if [ -z "${dst}" ]; then
  echo
  echo 'Could not auto-detect installation directory.'
  read -p 'Enter installation path to remove (e.g. /usr/local/lib/ippfix): ' dst
  if [ ! -f "${dst}/ippfix.py" ]; then
    echo "Warning: ippfix.py not found in \"${dst}\"."
    read -p 'Are you sure you want to delete this directory? [y/N] ' confirm
    [[ "${confirm}" =~ ^[Yy] ]] || dst=''
  fi
fi

if [ -n "${dst}" ] && [ -d "${dst}" ]; then
  echo -n "Removing files from \"${dst}\"..."
  if [[ "${dst}" == '/' ]] || [[ "${dst}" == '/usr' ]] || \
     [[ "${dst}" == '/usr/bin' ]] || [[ "${dst}" == '/home' ]]; then
    echo ' Skipped (unsafe path).'
  else
    rm -rf "${dst}"
    echo ' done.'
  fi
else
    echo 'Installation directory not found or skipped.'
fi

# Configuration, including the generated TLS credentials
if [ -d '/etc/ippfix' ]; then
  read -p 'Also remove /etc/ippfix and its TLS credentials? [y/N] ' confirm
  if [[ "${confirm}" =~ ^[Yy] ]]; then
    rm -rf '/etc/ippfix'
    echo 'Removed /etc/ippfix.'
  else
    echo 'Keeping /etc/ippfix.'
  fi
fi

# Delete user
echo -n 'Removing service users...'
for account in ippfix ippfix-convert; do
  if id "${account}" >&/dev/null; then
    userdel -r "${account}" >&/dev/null || :
  fi
done
echo ' done.'

echo -n 'Updating man database...'
mandb -q >&/dev/null || :
echo ' done.'

echo
echo 'Uninstall complete.'
