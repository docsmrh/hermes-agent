#!/usr/bin/env bash
# Install the VPS side of the Hermes Bridge for Transport Fever 3.
#
#   sudo ./install_vps.sh --pubkey-file FILE [--token-file FILE] [--tunnel-user tunnel] [--port 47800] [--hermes-user USER] [--dry-run]
#   sudo ./install_vps.sh --uninstall [--tunnel-user tunnel] [--hermes-user USER] [--dry-run]
#
# What it does (nothing else):
#   1. creates the tunnel account (no shell, no password) unless it exists;
#      an existing account must already have no login shell
#   2. installs the Mac's tunnel key (~/.ssh/hermes_tf3_tunnel.pub there) in
#      that account's authorized_keys, restricted to the one reverse tunnel
#   3. appends a marked Match block to /etc/ssh/sshd_config after a backup,
#      checks the result with `sshd -t` and the tunnel account's effective
#      policy with `sshd -T`, reloads sshd, and restores the backup if any of
#      that fails
#   4. copies the tf3 plugin into the Hermes user's ~/.hermes/plugins/tf3 and
#      enables it
#   5. stores TF3_BRIDGE_TOKEN in that user's ~/.hermes/.env (mode 0600), read
#      from --token-file or a hidden prompt, never from the command line
#
# --uninstall reverses 2-5 and leaves the account (remove it with userdel -r).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_SRC="$HERE/tf3"
SSHD_CONFIG="${SSHD_CONFIG:-/etc/ssh/sshd_config}"
SSHD="${SSHD:-$(command -v sshd || echo /usr/sbin/sshd)}"
BEGIN_MARK="# >>> hermes-tf3 tunnel (install_vps.sh) >>>"
END_MARK="# <<< hermes-tf3 tunnel <<<"
TOKEN_RE='^[A-Za-z0-9_-]{32,256}$'

TUNNEL_USER="tunnel"
PORT="47800"
HERMES_USER="${SUDO_USER:-}"
PUBKEY_FILE=""
TOKEN_FILE=""
DRY_RUN=0
UNINSTALL=0

usage() {
  sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

die() { echo "install_vps.sh: $*" >&2; exit 1; }

run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf 'would run:'; printf ' %q' "$@"; printf '\n'
  else
    "$@"
  fi
}

# The one authorized_keys line for the Mac's tunnel key. install_mac.sh
# prints the same line; a test keeps the two identical.
key_line() { # key_line <port> <public key>
  printf 'restrict,port-forwarding,permitlisten="127.0.0.1:%s",permitopen="127.0.0.1:1",command="/bin/false" %s\n' \
    "$1" "$2"
}

# The key options alone still allow unix-socket forwards; this block limits
# the account to the one tunnel. `Match all` ends it, so settings appended to
# the file later apply to everyone again instead of only to this account.
match_block() { # match_block <user> <port>
  cat <<EOF
$BEGIN_MARK
Match User $1
    AllowStreamLocalForwarding no
    AllowTcpForwarding remote
    PermitListen 127.0.0.1:$2
    PermitOpen none
    AllowAgentForwarding no
    X11Forwarding no
    PermitTTY no
    ForceCommand /bin/false
Match all
$END_MARK
EOF
}

strip_block() { # strip_block <file>: the file without our marked block
  awk -v b="$BEGIN_MARK" -v e="$END_MARK" '$0 == b {skip = 1; next} $0 == e {skip = 0; next} !skip' "$1"
}

# Check what sshd would actually enforce for the tunnel account, so another
# Match block that wins first (or a typo) can not leave it wider open.
check_effective() { # check_effective <config> <user> <port>
  local effective want
  effective="$("$SSHD" -T -f "$1" -C "user=$2,host=localhost,addr=127.0.0.1")" || return 1
  for want in "allowtcpforwarding remote" "permitlisten 127.0.0.1:$3" "permitopen none" \
              "allowstreamlocalforwarding no" "allowagentforwarding no" "x11forwarding no" \
              "permittty no" "forcecommand /bin/false"; do
    if ! grep -qxF "$want" <<<"$effective"; then
      echo "sshd would not enforce '$want' for $2 (another Match block may take precedence)" >&2
      return 1
    fi
  done
}

# Write <config> with our block replaced (or removed, without <user>), after
# `sshd -t` and the effective-policy check pass on the candidate. Prints the
# backup path. Nothing is changed if a check fails. (Callers test its status,
# which suspends `set -e` inside, so every step is checked explicitly.)
rewrite_sshd_config() { # rewrite_sshd_config <config> [<user> <port>]
  local config="$1" candidate backup last
  candidate="$(mktemp "$config.hermes-tf3.XXXXXX")" || return 1
  if ! strip_block "$config" >"$candidate" \
     || ! chmod --reference="$config" "$candidate"; then
    rm -f "$candidate"
    return 1
  fi
  chown --reference="$config" "$candidate" 2>/dev/null || true
  if [ $# -ge 3 ]; then
    last="$(tail -c 1 "$candidate")"
    { [ -z "$last" ] || echo; match_block "$2" "$3"; } >>"$candidate" || { rm -f "$candidate"; return 1; }
  fi
  if ! "$SSHD" -t -f "$candidate"; then
    rm -f "$candidate"
    echo "sshd rejected the new configuration; $config is unchanged" >&2
    return 1
  fi
  if [ $# -ge 3 ] && ! check_effective "$candidate" "$2" "$3"; then
    rm -f "$candidate"
    echo "$config is unchanged" >&2
    return 1
  fi
  backup="$config.hermes-tf3.bak.$(date +%Y%m%d%H%M%S)"
  if ! cp -p "$config" "$backup" || ! mv -f "$candidate" "$config"; then
    rm -f "$candidate"
    echo "could not replace $config; it is unchanged" >&2
    return 1
  fi
  echo "$backup"
}

reload_sshd() {
  local svc
  for svc in ssh sshd; do
    if systemctl cat "$svc.service" >/dev/null 2>&1; then
      if systemctl is-active --quiet "$svc.service"; then
        systemctl reload "$svc.service"
      else
        echo "$svc.service is not running (socket activation?); new connections read the new config"
      fi
      return 0
    fi
  done
  echo "no ssh or sshd systemd service found; reload sshd yourself" >&2
  return 1
}

apply_sshd() { # apply_sshd [<user> <port>]  (no args: remove our block)
  local backup
  if [ "$DRY_RUN" = 1 ]; then
    if [ $# -ge 2 ]; then
      echo "would append to $SSHD_CONFIG (after sshd -t and sshd -T checks, with a backup):"
      match_block "$1" "$2" | sed 's/^/    /'
    else
      echo "would remove the hermes-tf3 block from $SSHD_CONFIG (after sshd -t, with a backup)"
    fi
    return 0
  fi
  backup="$(rewrite_sshd_config "$SSHD_CONFIG" "$@")" || die "sshd configuration not changed"
  if ! reload_sshd; then
    cp -p "$backup" "$SSHD_CONFIG"
    reload_sshd || true
    die "could not reload sshd; restored $SSHD_CONFIG from $backup"
  fi
  echo "updated $SSHD_CONFIG (backup: $backup)"
}

nologin_shell() {
  local s
  for s in /usr/sbin/nologin /sbin/nologin /bin/false; do
    [ -x "$s" ] && { echo "$s"; return 0; }
  done
  echo /bin/false
}

ensure_tunnel_user() { # ensure_tunnel_user <user>
  local entry shell
  if entry="$(getent passwd "$1")"; then
    shell="${entry##*:}"
    case "$shell" in
      */nologin|*/false) echo "using existing account $1 (shell $shell)" ;;
      *) die "account $1 has the login shell $shell. The Match block turns off shell logins for the tunnel account, so use a dedicated one (--tunnel-user)." ;;
    esac
  else
    # '*' rather than a locked '!' password: no password can match it, and
    # sshd without PAM still accepts the key (it refuses locked accounts).
    run useradd --create-home --shell "$(nologin_shell)" --password '*' \
      --comment "Hermes TF3 tunnel (reverse port forward only)" "$1"
  fi
}

install_key() { # install_key <user> <port> <pubkey file>
  local home group keys pub blob tmp line
  [ "$(grep -c . "$3")" = 1 ] || die "$3 must hold exactly one public key line"
  ssh-keygen -l -f "$3" >/dev/null 2>&1 || die "$3 is not an SSH public key"
  pub="$(grep . "$3")"
  case "$pub" in ssh-*|ecdsa-*|sk-*) ;; *) die "$3 must be a bare public key (no options)" ;; esac
  blob="$(awk '{print $2}' <<<"$pub")"
  if [ "$DRY_RUN" = 1 ] && ! getent passwd "$1" >/dev/null; then
    echo "would install in ~$1/.ssh/authorized_keys:"; key_line "$2" "$pub" | sed 's/^/    /'
    return 0
  fi
  home="$(getent passwd "$1" | cut -d: -f6)"
  group="$(id -gn "$1")"
  keys="$home/.ssh/authorized_keys"
  if [ "$DRY_RUN" = 1 ]; then
    echo "would install in $keys:"; key_line "$2" "$pub" | sed 's/^/    /'
    return 0
  fi
  install -d -m 700 -o "$1" -g "$group" "$home/.ssh"
  tmp="$(mktemp "$keys.XXXXXX")"
  if [ -f "$keys" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in *" $blob "*|*" $blob") ;; *) printf '%s\n' "$line" ;; esac
    done <"$keys" >"$tmp"
  fi
  key_line "$2" "$pub" >>"$tmp"
  chmod 600 "$tmp"
  chown "$1:$group" "$tmp"
  mv -f "$tmp" "$keys"
  echo "installed the tunnel key in $keys"
}

remove_key() { # remove_key <user>: drop the lines install_key wrote
  local home keys tmp line
  getent passwd "$1" >/dev/null || return 0
  home="$(getent passwd "$1" | cut -d: -f6)"
  keys="$home/.ssh/authorized_keys"
  [ -f "$keys" ] || return 0
  if [ "$DRY_RUN" = 1 ]; then echo "would remove the tunnel key lines from $keys"; return 0; fi
  tmp="$(mktemp "$keys.XXXXXX")"
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in 'restrict,port-forwarding,permitlisten="127.0.0.1:'*) ;; *) printf '%s\n' "$line" ;; esac
  done <"$keys" >"$tmp"
  chmod 600 "$tmp"
  chown --reference="$keys" "$tmp"
  mv -f "$tmp" "$keys"
}

as_hermes() { # as_hermes <command string>: run in the Hermes user's login environment
  runuser -u "$HERMES_USER" -- env -i HOME="$HERMES_HOME_DIR" USER="$HERMES_USER" LOGNAME="$HERMES_USER" \
    PATH="$HERMES_HOME_DIR/.local/bin:/usr/local/bin:/usr/bin:/bin" HERMES_HOME="$HERMES_DIR" bash -lc "$1"
}

install_plugin() {
  local dest="$HERMES_DIR/plugins/tf3"
  if ! grep -qsx 'name: tf3' "$PLUGIN_SRC/plugin.yaml"; then
    die "the tf3 plugin is not next to this script ($PLUGIN_SRC)"
  fi
  if [ -e "$dest" ] && [ ! -L "$dest" ] && ! grep -qsx 'name: tf3' "$dest/plugin.yaml"; then
    die "$dest exists but is not the tf3 plugin; refusing to overwrite it"
  fi
  run install -d -m 700 -o "$HERMES_USER" -g "$HERMES_GROUP" "$HERMES_DIR" "$HERMES_DIR/plugins"
  run rm -rf "$dest"
  run cp -R "$PLUGIN_SRC" "$dest"
  run find "$dest" -name __pycache__ -prune -exec rm -rf {} +
  run chown -R "$HERMES_USER:$HERMES_GROUP" "$dest"
  if [ "$DRY_RUN" = 1 ]; then
    echo "would run as $HERMES_USER: hermes plugins enable tf3 --no-allow-tool-override"
  elif ! as_hermes 'command -v hermes >/dev/null && hermes plugins enable tf3 --no-allow-tool-override'; then
    echo "could not run 'hermes plugins enable tf3' as $HERMES_USER; run it yourself" >&2
  fi
}

remove_plugin() {
  local dest="$HERMES_DIR/plugins/tf3"
  if [ "$DRY_RUN" = 1 ]; then
    echo "would run as $HERMES_USER: hermes plugins disable tf3"
  else
    as_hermes 'command -v hermes >/dev/null && hermes plugins disable tf3' || true
  fi
  if [ -L "$dest" ] || grep -qsx 'name: tf3' "$dest/plugin.yaml"; then
    run rm -rf "$dest"
  fi
}

# Rewrite <env file> without TF3_BRIDGE_TOKEN, adding it back when a token is
# given. Only shell builtins see the token, so it never shows up in a process
# list.
write_env_token() { # write_env_token <env file> <owner> <group> [<token>]
  local env="$1" tmp line
  tmp="$(mktemp "$env.XXXXXX")"
  if [ -f "$env" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in TF3_BRIDGE_TOKEN=*|"export TF3_BRIDGE_TOKEN="*) ;; *) printf '%s\n' "$line" ;; esac
    done <"$env" >"$tmp"
  fi
  [ $# -lt 4 ] || printf 'TF3_BRIDGE_TOKEN=%s\n' "$4" >>"$tmp"
  chmod 600 "$tmp"
  chown "$2:$3" "$tmp"
  mv -f "$tmp" "$env"
}

read_token() { # prints the token, from --token-file or a hidden prompt
  local token=""
  if [ -n "$TOKEN_FILE" ]; then
    IFS= read -r token <"$TOKEN_FILE" || true
  elif [ -r /dev/tty ]; then
    printf 'TF3_BRIDGE_TOKEN (on the Mac: python3 ~/Library/Application\\ Support/hermes-tf3-bridge/tf3_relay.py token): ' >/dev/tty
    IFS= read -rs token </dev/tty || true
    printf '\n' >/dev/tty
  fi
  token="${token//[[:space:]]/}"
  [[ "$token" =~ $TOKEN_RE ]] || return 1
  printf '%s' "$token"
}

main() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --pubkey-file) PUBKEY_FILE="${2:?--pubkey-file needs a path}"; shift 2 ;;
      --token-file) TOKEN_FILE="${2:?--token-file needs a path}"; shift 2 ;;
      --tunnel-user) TUNNEL_USER="${2:?--tunnel-user needs a name}"; shift 2 ;;
      --port) PORT="${2:?--port needs a number}"; shift 2 ;;
      --hermes-user) HERMES_USER="${2:?--hermes-user needs a name}"; shift 2 ;;
      --dry-run) DRY_RUN=1; shift ;;
      --uninstall) UNINSTALL=1; shift ;;
      -h|--help) usage 0 ;;
      *) echo "unknown option: $1" >&2; usage 2 ;;
    esac
  done

  case "$PORT" in ''|*[!0-9]*) die "--port must be a number" ;; esac
  case "$TUNNEL_USER" in ''|*[!a-z0-9_-]*|-*) die "--tunnel-user must be a plain account name" ;; esac
  [ "$(id -u)" = 0 ] || [ "$DRY_RUN" = 1 ] || die "run it with sudo (it changes sshd and adds an account)"
  [ -n "$HERMES_USER" ] || die "--hermes-user is required (the account that runs Hermes)"
  getent passwd "$HERMES_USER" >/dev/null || die "no account named $HERMES_USER (--hermes-user)"
  case "$TUNNEL_USER" in
    root|"$HERMES_USER"|"${SUDO_USER:-}") die "the tunnel account must be a dedicated one, not $TUNNEL_USER" ;;
  esac
  HERMES_HOME_DIR="$(getent passwd "$HERMES_USER" | cut -d: -f6)"
  HERMES_GROUP="$(id -gn "$HERMES_USER")"
  HERMES_DIR="$HERMES_HOME_DIR/.hermes"

  if [ "$UNINSTALL" = 1 ]; then
    apply_sshd
    remove_key "$TUNNEL_USER"
    remove_plugin
    if [ "$DRY_RUN" = 1 ]; then
      echo "would remove TF3_BRIDGE_TOKEN from $HERMES_DIR/.env"
    elif [ -f "$HERMES_DIR/.env" ]; then
      write_env_token "$HERMES_DIR/.env" "$HERMES_USER" "$HERMES_GROUP"
    fi
    echo "Removed. The $TUNNEL_USER account is left in place: sudo userdel -r $TUNNEL_USER"
    return 0
  fi

  [ -n "$PUBKEY_FILE" ] || die "--pubkey-file is required (the Mac's ~/.ssh/hermes_tf3_tunnel.pub)"
  [ -f "$PUBKEY_FILE" ] || die "no such file: $PUBKEY_FILE"
  [ -z "$TOKEN_FILE" ] || [ -f "$TOKEN_FILE" ] || die "no such file: $TOKEN_FILE"

  local token=""
  if [ "$DRY_RUN" != 1 ]; then
    token="$(read_token)" || die "no valid token (43 URL-safe characters from 'tf3_relay.py token' on the Mac)"
  fi

  # The Match block goes in before the key, so the key never works on an
  # account that is not locked down yet.
  ensure_tunnel_user "$TUNNEL_USER"
  apply_sshd "$TUNNEL_USER" "$PORT"
  install_key "$TUNNEL_USER" "$PORT" "$PUBKEY_FILE"
  install_plugin
  if [ "$DRY_RUN" = 1 ]; then
    echo "would store TF3_BRIDGE_TOKEN in $HERMES_DIR/.env (mode 0600)"
  else
    write_env_token "$HERMES_DIR/.env" "$HERMES_USER" "$HERMES_GROUP" "$token"
    echo "stored TF3_BRIDGE_TOKEN in $HERMES_DIR/.env"
  fi

  cat <<EOF

Done on the VPS. Next:
  1. On the Mac, accept this VPS's host key once and start the tunnel
     (install_mac.sh printed both commands).
  2. Restart Hermes so it loads the plugin:  hermes gateway restart
  3. Check the bridge:                       hermes tf3 status
EOF
  [ -z "$TOKEN_FILE" ] || echo "You can delete $TOKEN_FILE now."
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
