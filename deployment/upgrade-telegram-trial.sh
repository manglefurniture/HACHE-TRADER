#!/usr/bin/env bash
# Upgrade the existing read-only Telegram service to a SHA-pinned
# automatic PAPER-only trial release (never enables real orders). Requires root and an already installed service.
# Usage: bash upgrade-telegram-trial.sh <new-git-sha> <archive.tar.gz> <sha256>
set -Eeuo pipefail
umask 077
die() { printf 'UPGRADE_REJECTED: %s\n' "$*" >&2; exit 1; }

[[ "${EUID}" -eq 0 ]] || die "root_required"
[[ "$#" -eq 3 ]] || die "expected_new_commit_archive_hash"
sha="$1"; archive="$2"; checksum="$3"
[[ "$sha" =~ ^[a-f0-9]{40}$ ]] || die "invalid_new_sha"
[[ "$checksum" =~ ^[a-f0-9]{64}$ ]] || die "invalid_sha256"
[[ "$archive" = /* && -f "$archive" && ! -L "$archive" ]] || die "invalid_archive"
unit="hache-trader-telegram-paper.service"
source_unit="/etc/systemd/system/$unit"
[[ -f "$source_unit" && ! -L "$source_unit" ]] || die "unit_not_installed"
grep -Fq "hache_trader observe --telegram --state-dir" "$source_unit" || die "previous_unit_not_readonly_observer"
[[ -L /opt/hache-trader/current ]] || die "release_link_missing"
old_target="$(readlink /opt/hache-trader/current)"
[[ "$old_target" =~ ^releases/[a-f0-9]{40}$ ]] || die "unexpected_release_target"
old_sha="${old_target#releases/}"
[[ "$old_sha" != "$sha" ]] || die "already_this_release"
[[ -d "/opt/hache-trader/$old_target" ]] || die "previous_release_missing"
[[ ! -e "/opt/hache-trader/releases/$sha" ]] || die "new_release_exists"
systemctl is-active --quiet "$unit" || die "previous_service_not_running"
[[ -L /var/lib/hache-trader-paper ]] || die "state_directory_unexpected"
[[ "$(readlink /var/lib/hache-trader-paper)" = "private/hache-trader-paper" ]] || die "state_target_unexpected"
[[ -d /var/lib/private/hache-trader-paper ]] || die "private_state_missing"
[[ -d /etc/hache-trader && ! -L /etc/hache-trader ]] || die "credentials_directory_unexpected"
for file in /etc/hache-trader/telegram.env /etc/hache-trader/telegram_token; do
    [[ -f "$file" && ! -L "$file" ]] || die "credential_file_unexpected"
    [[ "$(stat -c '%u:%a' "$file")" = "0:600" ]] || die "credential_permissions_invalid"
done

printf '%s  %s\n' "$checksum" "$archive" | sha256sum --check --status || die "checksum_invalid"
python3 - "$archive" <<'PY'
import sys, tarfile
with tarfile.open(sys.argv[1], mode="r:gz") as tar:
    names=set()
    for member in tar:
        name=member.name
        if (not name or name.startswith("/") or name.startswith("./")
                or ".." in name.split("/") or
                not (member.isfile() or member.isdir()) or name in names):
            raise SystemExit("unsafe_archive_member")
        names.add(name)
    required={"hache_trader/observer.py",
              "hache_trader/telegram.py",
              "hache_trader/__main__.py",
              "deployment/systemd/hache-trader-telegram-trial.service"}
    if not required <= names:
        raise SystemExit("required_files_missing")
print("ARCHIVE_STRUCTURE_OK")
PY

new_release="/opt/hache-trader/releases/$sha"
install -d -m 0755 "$new_release"
# A preflight rejection must not strand a partial release directory.
preflight_cleanup() {
    rc=$?
    trap - EXIT
    if [[ "$rc" -ne 0 ]]; then rm -rf -- "$new_release"; fi
}
trap preflight_cleanup EXIT
# Installation only; never edit existing versioned files in place.
tar -xzf "$archive" -C "$new_release" --no-same-owner --no-same-permissions
chown -R root:root "$new_release"
find "$new_release" -type d -exec chmod 0755 {} +
find "$new_release" -type f -exec chmod 0644 {} +
new_unit="$new_release/deployment/systemd/hache-trader-telegram-trial.service"
grep -Fq 'hache_trader trial --telegram --state-dir /var/lib/hache-trader-paper' "$new_unit" || die "unit_not_paper_trial"
bash -n "$new_release/deployment/install-telegram-paper.sh"
python3 -B -m compileall -q "$new_release/hache_trader" "$new_release/tests"
PYTHONPATH="$new_release" PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s "$new_release/tests" -q

# Preflight systemd unit with no changes to the current installed unit.
systemd-analyze verify "$new_unit" || die "new_unit_invalid"
install -d -m 0700 /opt/hache-trader/backups
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_dir="/opt/hache-trader/backups/upgrade-$stamp-$sha"
mkdir -m 0700 "$backup_dir"
cp -- "$source_unit" "$backup_dir/previous.service"
printf '%s\n' "$old_sha" > "$backup_dir/previous.sha"
# SQLite's online backup API reads the WAL-consistent snapshot; don't copy a
# live database file with cp and never discard the existing ledger on rollback.
python3 - "$backup_dir/ledger.sqlite" <<'PY'
import sqlite3,sys
source="file:/var/lib/private/hache-trader-paper/ledger.sqlite?mode=ro"
a=sqlite3.connect(source,uri=True,timeout=10)
b=sqlite3.connect(sys.argv[1])
try: a.backup(b)
finally: b.close(); a.close()
print("LEDGER_BACKUP_OK")
PY
chmod 0600 "$backup_dir/ledger.sqlite"

trap - EXIT
stopped=false
switched=false
unit_replaced=false
rollback() {
    rc=$?
    trap - ERR INT TERM HUP
    if [[ "$stopped" == true ]]; then
        systemctl stop "$unit" >/dev/null 2>&1 || :
    fi
    if [[ "$unit_replaced" == true ]]; then
        cp -- "$backup_dir/previous.service" "$source_unit"
        chmod 0644 "$source_unit"
    fi
    if [[ "$switched" == true ]]; then
        ln -sfnT "$old_target" /opt/hache-trader/current
    fi
    systemctl daemon-reload || :
    systemctl reset-failed "$unit" || :
    if [[ "$stopped" == true ]]; then
        systemctl start "$unit" || :
    fi
    rm -rf -- "$new_release"
    printf 'UPGRADE_ROLLED_BACK exit=%s previous=%s\n' "$rc" "$old_sha" >&2
    exit "$rc"
}
trap rollback ERR
trap 'false' INT TERM HUP
stopped=true
systemctl stop "$unit"
ln -sfnT "releases/$sha" /opt/hache-trader/current
switched=true
install -m 0644 "$new_unit" "$source_unit"
unit_replaced=true
systemctl daemon-reload
systemctl reset-failed "$unit"
systemctl start "$unit"
sleep 6
systemctl is-active --quiet "$unit"
[[ "$(readlink /opt/hache-trader/current)" = "releases/$sha" ]]
trap - ERR INT TERM HUP
printf 'UPGRADE_OK\nSHA=%s\nBACKUP=%s\n' "$sha" "$backup_dir"
printf 'BOT=ACTIVE\nPAPER_TRIAL=PAUSED_ON_START\nTRADING_REAL=DISABLED\n'
