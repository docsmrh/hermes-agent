"""install_vps.sh: the VPS side's sshd lockdown, tunnel key, token and dry run.

The script's functions are sourced and run against a real OpenSSH ``sshd``
in test mode (``sshd -t`` / ``sshd -T``) on throwaway configs. Nothing here
needs root or touches the machine's own accounts or /etc/ssh.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.plugins.tf3.harness import INTEGRATION

SCRIPT = INTEGRATION / "hermes-plugin" / "install_vps.sh"
MAC_SCRIPT = INTEGRATION / "tf3-mod" / "install_mac.sh"
SSHD = shutil.which("sshd") or "/usr/sbin/sshd"
TOKEN = "Abc_123-" + "x" * 35  # the shape of secrets.token_urlsafe(32)

needs_sshd = pytest.mark.skipif(not Path(SSHD).exists(), reason="OpenSSH sshd is not installed")


def sh(script: str, *, env: dict | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run bash with install_vps.sh's functions loaded."""
    full_env = {**os.environ, "SSHD": SSHD, **(env or {})}
    return subprocess.run(["bash", "-c", f'set -euo pipefail; source "{SCRIPT}"; {script}'],
                          capture_output=True, text=True, env=full_env, cwd=cwd, timeout=60)


@pytest.fixture
def pubkey(tmp_path):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "hermes-tf3-tunnel",
                    "-f", str(tmp_path / "tunnel")], check=True)
    return tmp_path / "tunnel.pub"


@pytest.fixture
def sshd_config(tmp_path):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "hostkey")], check=True)
    config = tmp_path / "sshd_config"
    config.write_text(f"HostKey {tmp_path / 'hostkey'}\nPidFile none\nPasswordAuthentication no\n")
    return config


def effective(config: Path, user: str) -> set:
    out = subprocess.run([SSHD, "-T", "-f", str(config), "-C", f"user={user},host=localhost,addr=127.0.0.1"],
                         capture_output=True, text=True, check=True).stdout
    return set(out.splitlines())


def test_the_key_line_matches_what_install_mac_prints(tmp_path, pubkey):
    """Both installers write the same restricted line for the tunnel key."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    shutil.copy(pubkey, home / ".ssh" / "hermes_tf3_tunnel.pub")
    mac = subprocess.run(["bash", str(MAC_SCRIPT), "--vps", "tunnel@vps.example.com", "--port", "47811",
                          "--userdata-dir", str(tmp_path / "ud"), "--dry-run"],
                         capture_output=True, text=True, env={**os.environ, "HOME": str(home)}, cwd=MAC_SCRIPT.parent)
    assert mac.returncode == 0, mac.stderr
    printed = {line.strip() for line in mac.stdout.splitlines() if line.strip().startswith("restrict,")}
    vps = sh(f'key_line 47811 "$(cat "{pubkey}")"').stdout.strip()
    assert printed == {vps}


@needs_sshd
def test_the_match_block_locks_the_tunnel_account_down(sshd_config, tmp_path):
    original = sshd_config.read_text()
    out = sh(f'rewrite_sshd_config "{sshd_config}" tunnel 47800')
    assert out.returncode == 0, out.stderr
    backup = Path(out.stdout.strip())
    assert backup.read_text() == original
    tunnel = effective(sshd_config, "tunnel")
    assert {"allowtcpforwarding remote", "permitlisten 127.0.0.1:47800", "permitopen none",
            "allowstreamlocalforwarding no", "permittty no", "forcecommand /bin/false"} <= tunnel
    other = effective(sshd_config, "admin")
    assert "forcecommand none" in other and "permittty yes" in other  # nobody else is affected
    # Settings appended to the file later apply to everyone, not only to the tunnel account.
    with sshd_config.open("a") as f:
        f.write("X11Forwarding yes\n")
    assert "x11forwarding yes" in effective(sshd_config, "admin")


@needs_sshd
def test_reinstall_replaces_the_block_and_uninstall_removes_it(sshd_config):
    original = sshd_config.read_text()
    assert sh(f'rewrite_sshd_config "{sshd_config}" tunnel 47800').returncode == 0
    assert sh(f'rewrite_sshd_config "{sshd_config}" tunnel 47801').returncode == 0
    text = sshd_config.read_text()
    assert text.count("Match User tunnel") == 1 and "PermitListen 127.0.0.1:47801" in text
    assert sh(f'rewrite_sshd_config "{sshd_config}"').returncode == 0
    assert sshd_config.read_text() == original


@needs_sshd
def test_an_earlier_match_block_that_wins_leaves_the_config_unchanged(sshd_config, tmp_path):
    """sshd uses the first value it finds, so an earlier block would override
    ours; the effective-policy check catches that before anything changes."""
    with sshd_config.open("a") as f:
        f.write("Match User tunnel\n    AllowTcpForwarding yes\n")
    before = sshd_config.read_bytes()
    out = sh(f'rewrite_sshd_config "{sshd_config}" tunnel 47800')
    assert out.returncode != 0 and "allowtcpforwarding remote" in out.stderr
    assert sshd_config.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir() if "hermes-tf3" in p.name) == []  # no backup, no temp file


@needs_sshd
def test_a_config_sshd_rejects_is_left_alone(sshd_config, tmp_path):
    with sshd_config.open("a") as f:
        f.write("NoSuchDirective yes\n")
    before = sshd_config.read_bytes()
    out = sh(f'rewrite_sshd_config "{sshd_config}" tunnel 47800')
    assert out.returncode != 0 and "unchanged" in out.stderr
    assert sshd_config.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir() if "hermes-tf3" in p.name] == []


def _fake_account(tmp_path):
    """Shell overrides so install_key/remove_key write under tmp_path instead of
    a real account's home (they run as the current user, who owns it)."""
    home = tmp_path / "tunnelhome"
    home.mkdir()
    return home, (
        'ME="$(command id -un)"; '
        f'getent() {{ printf "%s:x:%s:%s::%s:/usr/sbin/nologin\\n" "$ME" "$(command id -u)" "$(command id -g)" "{home}"; }}; '
    )


def test_the_tunnel_key_is_installed_once_and_removed_cleanly(tmp_path, pubkey):
    home, fake = _fake_account(tmp_path)
    keys = home / ".ssh" / "authorized_keys"
    keys.parent.mkdir(mode=0o700)
    blob = pubkey.read_text().split()[1]
    keys.write_text(f"ssh-ed25519 OTHERKEY admin@laptop\nrestrict,port-forwarding ssh-ed25519 {blob} old\n")
    for port in (47800, 47801):  # a re-run replaces the line instead of adding another
        out = sh(fake + f'install_key "$ME" {port} "{pubkey}"')
        assert out.returncode == 0, out.stderr
    lines = keys.read_text().splitlines()
    assert lines[0] == "ssh-ed25519 OTHERKEY admin@laptop"
    assert len([line for line in lines if blob in line]) == 1 and 'permitlisten="127.0.0.1:47801"' in lines[-1]
    assert oct(keys.stat().st_mode & 0o777) == "0o600"
    assert sh(fake + 'remove_key "$ME"').returncode == 0
    assert keys.read_text().splitlines() == ["ssh-ed25519 OTHERKEY admin@laptop"]


def test_only_a_bare_public_key_is_accepted(tmp_path, pubkey):
    _, fake = _fake_account(tmp_path)
    with_options = tmp_path / "opts.pub"
    with_options.write_text('command="/bin/sh" ' + pubkey.read_text())
    two = tmp_path / "two.pub"
    two.write_text(pubkey.read_text() * 2)
    junk = tmp_path / "junk.pub"
    junk.write_text("not a key\n")
    for bad in (with_options, two, junk):
        out = sh(fake + f'install_key "$ME" 47800 "{bad}"')
        assert out.returncode != 0, bad


def test_the_token_replaces_an_old_one_and_stays_private(tmp_path):
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=keep-me\nexport TF3_BRIDGE_TOKEN=old\n# comment\n")
    out = sh(f'write_env_token "{env}" "$(id -un)" "$(id -gn)" "{TOKEN}"')
    assert out.returncode == 0, out.stderr
    assert env.read_text() == f"OPENAI_API_KEY=keep-me\n# comment\nTF3_BRIDGE_TOKEN={TOKEN}\n"
    assert oct(env.stat().st_mode & 0o777) == "0o600"
    assert sh(f'write_env_token "{env}" "$(id -un)" "$(id -gn)"').returncode == 0
    assert env.read_text() == "OPENAI_API_KEY=keep-me\n# comment\n"


def test_token_files_are_validated(tmp_path):
    good = tmp_path / "token"
    good.write_text(TOKEN + "\r\n")
    out = sh(f'TOKEN_FILE="{good}"; read_token')
    assert out.returncode == 0 and out.stdout == TOKEN
    bad = tmp_path / "bad"
    bad.write_text("short\n")
    assert sh(f'TOKEN_FILE="{bad}"; read_token').returncode != 0


def test_dry_run_changes_nothing_and_shows_the_plan(tmp_path, pubkey):
    me = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
    config = tmp_path / "sshd_config"
    config.write_text("PasswordAuthentication no\n")
    out = subprocess.run(["bash", str(SCRIPT), "--dry-run", "--pubkey-file", str(pubkey), "--hermes-user", me,
                          "--tunnel-user", "tf3tunnel-test"], capture_output=True, text=True,
                         env={**os.environ, "SSHD_CONFIG": str(config), "SSHD": SSHD}, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "would run: useradd" in out.stdout and "Match User tf3tunnel-test" in out.stdout
    assert 'permitlisten="127.0.0.1:47800"' in out.stdout and "hermes plugins enable tf3" in out.stdout
    assert config.read_text() == "PasswordAuthentication no\n"


@pytest.mark.parametrize("tunnel_user", ["root", "HERMES_USER"])
def test_refuses_root_or_the_hermes_account_as_the_tunnel(tmp_path, pubkey, tunnel_user):
    """The Match block turns off shell logins for the tunnel account."""
    me = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
    out = subprocess.run(["bash", str(SCRIPT), "--dry-run", "--pubkey-file", str(pubkey), "--hermes-user", me,
                          "--tunnel-user", me if tunnel_user == "HERMES_USER" else tunnel_user],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode != 0 and "dedicated" in out.stderr


def test_an_existing_account_must_have_no_login_shell():
    login = sh('getent() { echo "alice:x:1001:1001::/home/alice:/bin/bash"; }; ensure_tunnel_user alice')
    assert login.returncode != 0 and "login shell /bin/bash" in login.stderr
    service = sh('getent() { echo "tunnel:x:1002:1002::/home/tunnel:/usr/sbin/nologin"; }; ensure_tunnel_user tunnel')
    assert service.returncode == 0 and "using existing account tunnel" in service.stdout
