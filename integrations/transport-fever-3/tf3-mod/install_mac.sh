#!/usr/bin/env bash
# Install the Hermes Bridge for Transport Fever 3 on a Mac.
#
#   ./install_mac.sh --vps tunnel@vps.example.com [--userdata-dir DIR] [--port 47800] [--dry-run]
#   ./install_mac.sh --uninstall [--dry-run]
#
# What it does (nothing else):
#   1. copies the hermes_bridge mod into <TF3 userdata>/mods/hermes_bridge
#   2. copies tf3_relay.py to ~/Library/Application Support/hermes-tf3-bridge/
#      and runs `tf3_relay.py init` (config + token, mode 0600)
#   3. creates ~/.ssh/hermes_tf3_tunnel (ed25519, tunnel-only key) if missing
#   4. installs two per-user LaunchAgents: the relay, and an SSH reverse
#      tunnel that makes the relay reachable as 127.0.0.1:<port> on the VPS
#   5. prints the restricted authorized_keys line to add on the VPS
#
# The relay listens on 127.0.0.1 only; no port is opened on the Mac.

set -euo pipefail

LABEL_RELAY="com.hermes.tf3-relay"
LABEL_TUNNEL="com.hermes.tf3-tunnel"
SUPPORT_DIR="$HOME/Library/Application Support/hermes-tf3-bridge"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs"
KEY="$HOME/.ssh/hermes_tf3_tunnel"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MOD_SRC="$HERE/hermes_bridge"
RELAY_SRC="$HERE/relay/tf3_relay.py"

VPS=""
USERDATA=""
PORT="47800"
DRY_RUN=0
UNINSTALL=0

usage() {
  sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --vps) VPS="${2:?--vps needs user@host}"; shift 2 ;;
    --userdata-dir) USERDATA="${2:?--userdata-dir needs a path}"; shift 2 ;;
    --port) PORT="${2:?--port needs a number}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown option: $1" >&2; usage 2 ;;
  esac
done

run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf 'would run:'; printf ' %q' "$@"; printf '\n'
  else
    "$@"
  fi
}

write_file() { # write_file <path> <mode>  (content on stdin)
  if [ "$DRY_RUN" = 1 ]; then
    echo "would write $1 (mode $2):"; sed 's/^/    /'
  else
    local tmp
    tmp="$(mktemp "$1.XXXXXX")"
    cat >"$tmp"
    chmod "$2" "$tmp"
    mv -f "$tmp" "$1"
  fi
}

xml_escape() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

agent_unload() {
  local label="$1"
  if [ "$DRY_RUN" = 1 ]; then echo "would unload LaunchAgent $label"; return 0; fi
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
}

agent_load() {
  local plist="$1"
  if [ "$DRY_RUN" = 1 ]; then echo "would load LaunchAgent $plist"; return 0; fi
  launchctl bootstrap "gui/$(id -u)" "$plist"
}

if [ "$(uname -s)" != "Darwin" ] && [ "$DRY_RUN" != 1 ]; then
  echo "This installer is for macOS (use --dry-run to preview elsewhere)." >&2
  exit 1
fi

case "$PORT" in ''|*[!0-9]*) echo "--port must be a number" >&2; exit 2 ;; esac

if [ "$UNINSTALL" = 1 ]; then
  agent_unload "$LABEL_TUNNEL"
  agent_unload "$LABEL_RELAY"
  run rm -f "$AGENTS_DIR/$LABEL_TUNNEL.plist" "$AGENTS_DIR/$LABEL_RELAY.plist"
  echo "LaunchAgents removed. Left in place (delete by hand if you want):"
  echo "  $SUPPORT_DIR   (config + token)"
  echo "  $KEY(.pub)     (tunnel key; also remove its line from the VPS authorized_keys)"
  echo "  the hermes_bridge folder in your TF3 mods directory"
  exit 0
fi

[ -n "$VPS" ] || { echo "--vps user@host is required (the machine running Hermes)" >&2; exit 2; }
case "$VPS" in *[!A-Za-z0-9@._-]*) echo "--vps must look like user@host" >&2; exit 2 ;; esac
[ -f "$MOD_SRC/mod.json" ] && [ -f "$RELAY_SRC" ] || { echo "run this from the tf3-mod folder of the repo" >&2; exit 2; }

PYTHON="/usr/bin/python3"
if [ "$DRY_RUN" != 1 ] && ! "$PYTHON" -c 'import sys; assert sys.version_info >= (3, 9)' 2>/dev/null; then
  echo "Python 3.9+ is needed: run 'xcode-select --install' (Command Line Tools), then re-run." >&2
  exit 1
fi

# 1. Locate the TF3 userdata folder.
if [ -z "$USERDATA" ]; then
  candidates=()
  for d in "$HOME/Library/Application Support/Steam/userdata/"*/3493540/local "$HOME/Library/Application Support/Transport Fever 3"; do
    [ -d "$d" ] && candidates+=("$d")
  done
  if [ "${#candidates[@]}" -ne 1 ]; then
    echo "Found ${#candidates[@]} TF3 userdata folders; pass one with --userdata-dir:" >&2
    for d in "${candidates[@]+"${candidates[@]}"}"; do echo "  $d" >&2; done
    echo "(In game: Settings > Advanced > Open User Data Folder.)" >&2
    [ "$DRY_RUN" = 1 ] || exit 2
    USERDATA="${candidates[0]:-$HOME/Library/Application Support/Steam/userdata/<id>/3493540/local}"
  else
    USERDATA="${candidates[0]}"
  fi
fi
echo "TF3 userdata: $USERDATA"

# 2. Mod.
run mkdir -p "$USERDATA/mods"
if [ -e "$USERDATA/mods/hermes_bridge" ] && [ ! -f "$USERDATA/mods/hermes_bridge/mod.json" ]; then
  echo "$USERDATA/mods/hermes_bridge exists but is not this mod; refusing to overwrite it." >&2
  exit 1
fi
run rm -rf "$USERDATA/mods/hermes_bridge"
run cp -R "$MOD_SRC" "$USERDATA/mods/hermes_bridge"

# 3. Relay + config.
run mkdir -p "$SUPPORT_DIR" "$LOG_DIR"
run chmod 700 "$SUPPORT_DIR"
run cp "$RELAY_SRC" "$SUPPORT_DIR/tf3_relay.py"
run "$PYTHON" "$SUPPORT_DIR/tf3_relay.py" init --userdata-dir "$USERDATA" --port "$PORT"

# 4. Tunnel key (used only for the reverse tunnel, never for a shell).
if [ ! -f "$KEY" ]; then
  run mkdir -p "$HOME/.ssh"
  run chmod 700 "$HOME/.ssh"
  run ssh-keygen -q -t ed25519 -N "" -C "hermes-tf3-tunnel" -f "$KEY"
fi

# 5. LaunchAgents.
run mkdir -p "$AGENTS_DIR"
agent_unload "$LABEL_RELAY"
agent_unload "$LABEL_TUNNEL"

write_file "$AGENTS_DIR/$LABEL_RELAY.plist" 644 <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL_RELAY</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$(xml_escape "$SUPPORT_DIR/tf3_relay.py")</string>
    <string>serve</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$(xml_escape "$LOG_DIR/hermes-tf3-relay.log")</string>
  <key>StandardErrorPath</key><string>$(xml_escape "$LOG_DIR/hermes-tf3-relay.log")</string>
</dict>
</plist>
EOF

write_file "$AGENTS_DIR/$LABEL_TUNNEL.plist" 644 <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL_TUNNEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/ssh</string>
    <string>-N</string>
    <string>-T</string>
    <string>-o</string><string>BatchMode=yes</string>
    <string>-o</string><string>IdentitiesOnly=yes</string>
    <string>-o</string><string>ExitOnForwardFailure=yes</string>
    <string>-o</string><string>ServerAliveInterval=30</string>
    <string>-o</string><string>ServerAliveCountMax=3</string>
    <string>-o</string><string>StrictHostKeyChecking=yes</string>
    <string>-i</string><string>$(xml_escape "$KEY")</string>
    <string>-R</string><string>127.0.0.1:$PORT:127.0.0.1:$PORT</string>
    <string>$VPS</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$(xml_escape "$LOG_DIR/hermes-tf3-tunnel.log")</string>
  <key>StandardErrorPath</key><string>$(xml_escape "$LOG_DIR/hermes-tf3-tunnel.log")</string>
</dict>
</plist>
EOF

agent_load "$AGENTS_DIR/$LABEL_RELAY.plist"

PUBKEY="$( [ -f "$KEY.pub" ] && cat "$KEY.pub" || echo "ssh-ed25519 AAAA...(generated on install) hermes-tf3-tunnel")"
TUNNEL_USER="${VPS%%@*}"
[ "$TUNNEL_USER" = "$VPS" ] && TUNNEL_USER="<tunnel-user>"
cat <<EOF

Done on the Mac. Finish on the VPS. Use a dedicated low-privilege account for
the tunnel (e.g. 'sudo useradd -m -s /usr/sbin/nologin $TUNNEL_USER'), never one
you or Hermes log in with: step 2 turns off shell logins for '$TUNNEL_USER'.

  1. Append this single line to that account's ~/.ssh/authorized_keys:

     restrict,port-forwarding,permitlisten="127.0.0.1:$PORT",permitopen="127.0.0.1:1",command="/bin/false" $PUBKEY

  2. As root, add this block at the END of /etc/ssh/sshd_config and reload sshd
     (sshd -t && systemctl reload sshd). The key options alone still allow
     unix-socket forwards; this block limits the account to the one tunnel:

     Match User $TUNNEL_USER
         AllowStreamLocalForwarding no
         AllowTcpForwarding remote
         PermitListen 127.0.0.1:$PORT
         PermitOpen none
         AllowAgentForwarding no
         X11Forwarding no
         PermitTTY no
         ForceCommand /bin/false

  3. Put the shared token in the Hermes .env on the VPS (print it on the Mac
     with: $PYTHON "$SUPPORT_DIR/tf3_relay.py" token). It is an HMAC key: it is
     never sent over the tunnel.

     TF3_BRIDGE_TOKEN=<token>

  4. Trust the VPS host key once from this Mac (the tunnel uses
     StrictHostKeyChecking=yes):   ssh -i "$KEY" -o IdentitiesOnly=yes $VPS true
     (it exits with an error from /bin/false after accepting the key; that is expected)

  5. Start the tunnel:
     launchctl bootstrap gui/\$(id -u) "$AGENTS_DIR/$LABEL_TUNNEL.plist"

  6. Restart Transport Fever 3, enable "Hermes Bridge" in the mod list when you
     start or load a game, then on the VPS run:  hermes tf3 status

Kill switch on the Mac at any time:  $PYTHON "$SUPPORT_DIR/tf3_relay.py" pause
EOF
