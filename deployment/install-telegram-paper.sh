#!/usr/bin/env bash
# Install a SHA-pinned HACHE-TRADER Telegram-only paper service from a verified archive.
# Usage (as root): bash install-telegram-paper.sh <40-char-git-commit> <archive.tar.gz> <sha256>
# Never obtains credentials or runs a real-money trading engine.
set -Eeuo pipefail
umask 022

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ "${EUID}" -eq 0 ]] || die "root_required"
[[ "$#" -eq 3 ]] || die "usage: <commit> <archive> <sha256>"
sha="$1"
archive="$2"
checksum="$3"
[[ "$sha" =~ ^[a-f0-9]{40}$ ]] || die "invalid_commit_sha"
[[ "$checksum" =~ ^[a-f0-9]{64}$ ]] || die "invalid_archive_sha256"
[[ "$archive" = /* && -f "$archive" && ! -L "$archive" ]] || die "invalid_archive_path"
[[ -d /etc/hache-trader && ! -L /etc/hache-trader ]] || die "missing_credentials_directory"
[[ "$(stat -c '%u:%a' /etc/hache-trader)" = "0:700" ]] || die "credentials_directory_permissions"

for file in /etc/hache-trader/telegram_token /etc/hache-trader/telegram.env; do
    [[ -f "$file" && ! -L "$file" ]] || die "missing_or_symlink_credential"
    [[ "$(stat -c '%u:%a' "$file")" = "0:600" ]] || die "credential_file_permissions"
done
grep -qx 'HACHE_TRADER_BOT_ID=9297293373' /etc/hache-trader/telegram.env || die "incorrect_bot_id"
grep -qx 'HACHE_TRADER_CHAT_ID=5074130395' /etc/hache-trader/telegram.env || die "incorrect_chat_id"
grep -qx 'HACHE_TRADER_USER_ID=5074130395' /etc/hache-trader/telegram.env || die "incorrect_user_id"

unit="hache-trader-telegram-paper.service"
[[ ! -e /etc/systemd/system/"$unit" ]] || die "service_already_installed"
[[ ! -e /opt/hache-trader/current && ! -L /opt/hache-trader/current ]] || die "release_already_active"
[[ ! -e /var/lib/hache-trader-paper && ! -L /var/lib/hache-trader-paper ]] || die "state_directory_already_exists"
[[ ! -e /var/lib/private/hache-trader-paper ]] || die "private_state_already_exists"
systemctl is-active --quiet hache-trader-paper.service && die "other_paper_service_active"
systemctl is-active --quiet "$unit" && die "telegram_service_active"

printf '%s  %s\n' "$checksum" "$archive" | sha256sum --check --status || die "release_checksum_mismatch"
# The release archive is produced by 'git archive' from the exact merge commit.
# Reject archives with absolute paths, parent traversal, links or device files.
python3 - "$archive" <<'PY'
import os,sys,tarfile
archive=sys.argv[1]
with tarfile.open(archive,mode="r:gz") as tar:
    names=set()
    for member in tar:
        p=member.name
        if p.startswith("/") or not p or ".." in p.split("/") or p.startswith("./"):
            raise SystemExit("unsafe_archive_path")
        if not (member.isfile() or member.isdir()):
            raise SystemExit("unsafe_archive_member")
        if p in names:
            raise SystemExit("duplicate_archive_member")
        names.add(p)
    for required in ("hache_trader/__main__.py", "hache_trader/telegram.py",
                     "hache_trader/domain.py",
                     "deployment/systemd/hache-trader-telegram-paper.service"):
        if required not in names:
            raise SystemExit("required_source_missing")
print("ARCHIVE_STRUCTURE_OK")
PY

release="/opt/hache-trader/releases/$sha"
[[ ! -e "$release" ]] || die "release_path_exists"
install -d -m 0755 /opt/hache-trader /opt/hache-trader/releases
install -d -m 0755 "$release"
installed_unit=false
installed_symlink=false

rollback() {
    rc=$?
    trap - ERR
    if [[ "$installed_unit" == true ]]; then
        systemctl disable --now "$unit" >/dev/null 2>&1 || :
        rm -f -- "/etc/systemd/system/$unit"
        systemctl daemon-reload || :
    fi
    if [[ "$installed_symlink" == true ]]; then
        rm -f -- /opt/hache-trader/current
    fi
    printf 'INSTALL_FAILED: rolled back service configuration (exit %s)\n' "$rc" >&2
    exit "$rc"
}
trap rollback ERR

tar -xzf "$archive" -C "$release" --no-same-owner --no-same-permissions
chown -R root:root "$release"
find "$release" -type d -exec chmod 0755 {} +
find "$release" -type f -exec chmod 0644 {} +
ln -s "releases/$sha" /opt/hache-trader/current
installed_symlink=true
python3 -B -m compileall -q "$release/hache_trader" "$release/tests"
PYTHONPATH="$release" python3 -B -m unittest discover -s "$release/tests" -v >/dev/null
install -m 0644 "$release/deployment/systemd/$unit" "/etc/systemd/system/$unit"
installed_unit=true
systemctl daemon-reload
systemctl enable --now "$unit"
sleep 3
systemctl is-active --quiet "$unit" || die "service_not_active"
trap - ERR
printf 'DEPLOY_OK: Telegram control-only simulation service active\nCOMMIT: %s\n' "$sha"
printf 'SERVICE: %s\n' "$unit"
printf 'TRADING_REAL: DISABLED\nMARKET_WORKER: DISABLED\n'
