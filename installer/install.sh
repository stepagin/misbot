#!/usr/bin/env bash
# Usage: sudo bash install.sh init example.org [/opt/matrix-stack]
#        sudo bash /opt/matrix-stack/install.sh up
set -euo pipefail
umask 077
task_source="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
task_action="${1:-help}"
die() { printf '%s\n' "$*" >&2; exit 1; }
if [[ "$task_action" == version || "$task_action" == --version ]]; then
  python3 -c 'import json,sys; print("Misbot " + json.load(open(sys.argv[1]))["version"])' "$task_source/release.json"
  exit 0
fi
if [[ "$task_action" == help || "$task_action" == --help ]]; then
  cat <<'EOF'
init DOMAIN [DIRECTORY]   Create settings/server.env, private server secrets, installation files.
up [DIRECTORY]           Install Docker if absent, generate/validate configs, start stack.
update [DIRECTORY]       Apply this source release to an existing portable installation.
update-recover [DIRECTORY]  Recover an interrupted source update from its private checkpoint.
version                  Show the Misbot product version.
generate [DIRECTORY]     Validate current configuration and generate runtime files, without restarting services.
preflight [DIRECTORY]    Check system, DNS, ports, subnet and optional Telegram proxy.
check [DIRECTORY]        Verify DNS, discovery, HTTPS, RTC and container state.
backup [DIRECTORY]       Consistent backup with short writer stop; service state restored.
backup-recover [DIRECTORY]  Recover writer state after a killed backup; migration sources stay stopped.
backup-schedule PRIVATE_S3_ENV KEY_FILE CALENDAR [DIRECTORY]  Verify a full external backup and enable its systemd timer.
backup-schedule-local CALENDAR [DIRECTORY]  Verify a local-media backup and enable its systemd timer.
backup-schedule-status [DIRECTORY]  Show next/last timer event and backup service result.
backup-schedule-disable [DIRECTORY]  Disable the timer; if an operation is busy, wait and retry.
backup-key KEY_FILE      Create a private recovery key; keep an independent copy off this VPS.
backup-external PRIVATE_S3_ENV KEY_FILE [DIRECTORY]  Encrypt a full backup, upload and verify download.
backup-cleanup PRIVATE_S3_ENV PRIVATE_CATALOG [DIRECTORY]  Retry deletion of this installation's unique unverified upload.
backup-fetch PRIVATE_S3_ENV KEY_FILE OBJECT_KEY SHA256 OUTPUT_ARCHIVE  Download and authenticate a backup into a new private file.
restore-check [DIRECTORY] Verify latest backup by restoring two temporary databases.
restore ARCHIVE DIRECTORY SHA256 PUBLIC_IP  Restore local databases into an empty staged target.
restore-local ARCHIVE DIRECTORY SHA256 PUBLIC_IP  Restore either database source into new local PostgreSQL; writers stay staged.
restore-activate source-stopped [DIRECTORY]  Start restored writers after stopping the source and switching DNS.
move-export [DIRECTORY]  Freeze source writers and create the final consistent archive.
move-abort target-stopped [DIRECTORY]  Resume the source after stopping the target and returning DNS.
media-local [DIRECTORY]  Use local media before the first successful installation.
media-s3 PRIVATE_S3_ENV [DIRECTORY]  Connect S3, copying existing media without deleting local files.
media-recover [DIRECTORY]  Recover local settings after an interrupted media transfer.
database-external PRIVATE_POSTGRES_ENV [DIRECTORY]  Move existing local PostgreSQL data to a dedicated TLS cluster.
database-recover [DIRECTORY]  Resume a pending database move without starting independent writers.
database-abort [DIRECTORY]  Return to local PostgreSQL only before destination activation.
database-password [DIRECTORY]  Generate new Synapse/MAS database passwords after a verified backup.
database-password-recover [DIRECTORY]  Complete an interrupted database password change using its private checkpoint.
s3-credentials PRIVATE_S3_ENV [DIRECTORY]  Replace media S3 credentials for the same destination after a verified backup.
s3-credentials-recover [DIRECTORY]  Complete an interrupted media S3 credentials change from its private checkpoint.
s3-credentials-cleanup [DIRECTORY]  Delete only owned credential-check objects recorded in private catalogs.
admin-password PRIVATE_JSON [DIRECTORY]  Change the initial administrator password with backup and recovery.
admin-password-recover [DIRECTORY]  Restore the saved administrator password after an interrupted change.
user-password PRIVATE_JSON [DIRECTORY]  Set an existing ordinary user's password using a private file and verified backup.
user-password-recover [DIRECTORY]  Confirm the chosen new user password after an interrupted reset.
vault-connect PRIVATE_VAULT_ENV [DIRECTORY]  Snapshot private secret files into a dedicated Vault KV v2 path.
vault-sync [DIRECTORY]   Verify and update the Vault snapshot; local runtime files are retained.
vault-recover [DIRECTORY]  Recover missing secret files from the verified version, with services stopped.
vault-auth PRIVATE_VAULT_ENV [DIRECTORY]  Change AppRole files for the same Vault destination.
vault-disconnect [DIRECTORY]  Continue with private local files, retaining remote versions.
status [DIRECTORY]       Show current container state.
stop [DIRECTORY]         Stop containers without deleting data.
down [DIRECTORY]         Remove owned containers/network; keep data, secrets and recovery checkpoints.
Default directory: /opt/matrix-stack; for installed script: its own directory.
EOF
  exit 0
fi
[[ "$(id -u)" == 0 ]] || die 'Run this installer with sudo or as root.'
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
unset DOCKER_CONTEXT
export DOCKER_HOST=unix:///var/run/docker.sock
task_root="$task_source"
[[ -f "$task_root/installation.json" ]] || task_root=/opt/matrix-stack
task_default_root="$task_root"
if [[ "$task_action" == init ]]; then
  task_domain="${2:-}"
  if [[ -z "$task_domain" ]]; then read -r -p 'Matrix domain (example.org): ' task_domain; fi
  task_root="${3:-/opt/matrix-stack}"
  task_storage_args=()
  if [[ ! -e "$task_root/settings.env" && ! -e "$task_root/settings/server.env" ]]; then
    task_database=local
    task_media=local
    task_storage_args=(--database-mode "$task_database" --media-mode "$task_media")
  fi
else
  task_root="${2:-$task_root}"
fi
if [[ "$task_action" == restore || "$task_action" == restore-local ]]; then
  [[ $# == 5 ]] || die 'Use: install.sh restore|restore-local ARCHIVE DIRECTORY SHA256 PUBLIC_IP'
  task_archive="$2"
  task_root="$3"
  task_digest="$4"
  task_public_ip="$5"
elif [[ "$task_action" == restore-activate || "$task_action" == move-abort ]]; then
  task_confirmation=source-stopped
  [[ "$task_action" != move-abort ]] || task_confirmation=target-stopped
  [[ "${2:-}" == "$task_confirmation" ]] || die "First stop the other writer and point DNS to this machine; use $task_action $task_confirmation [DIRECTORY]."
  task_root="${3:-$task_default_root}"
elif [[ "$task_action" == media-s3 || "$task_action" == s3-credentials ]]; then
  [[ $# == 2 || $# == 3 ]] || die "Use: install.sh $task_action PRIVATE_S3_ENV [DIRECTORY]"
  task_s3_file="${2:-}"
  [[ -f "$task_s3_file" ]] || die "Specify the private S3 env file on this VPS: install.sh $task_action PRIVATE_S3_ENV [DIRECTORY]"
  task_root="${3:-$task_default_root}"
elif [[ "$task_action" == database-external ]]; then
  [[ $# == 2 || $# == 3 ]] || die 'Use: install.sh database-external PRIVATE_POSTGRES_ENV [DIRECTORY]'
  task_postgres_file="$2"
  [[ -f "$task_postgres_file" ]] || die 'Create the private PostgreSQL env file on this VPS first; see storage.md.'
  task_root="${3:-$task_default_root}"
elif [[ "$task_action" == admin-password || "$task_action" == user-password ]]; then
  task_password_file="${2:-}"
  [[ $# == 2 || $# == 3 ]] || die "Use: install.sh $task_action PRIVATE_JSON [DIRECTORY]"
  [[ -f "$task_password_file" ]] || die "Create a root-owned mode-600 private JSON file first; see credentials.md or users.md. Then repeat install.sh $task_action PRIVATE_JSON [DIRECTORY]."
  task_root="${3:-$task_default_root}"
elif [[ "$task_action" == backup-key ]]; then
  [[ $# == 2 ]] || die 'Use: install.sh backup-key KEY_FILE'
  task_backup_key="$2"
elif [[ "$task_action" == vault-connect || "$task_action" == vault-auth ]]; then
  [[ $# == 2 || $# == 3 ]] || die 'Use: install.sh vault-connect|vault-auth PRIVATE_VAULT_ENV [DIRECTORY]'
  task_vault_env="$2"
  task_root="${3:-$task_default_root}"
elif [[ "$task_action" == backup-external ]]; then
  [[ $# == 3 || $# == 4 ]] || die 'Use: install.sh backup-external PRIVATE_S3_ENV KEY_FILE [DIRECTORY]'
  task_backup_env="$2"
  task_backup_key="$3"
  task_root="${4:-$task_default_root}"
elif [[ "$task_action" == backup-cleanup ]]; then
  [[ $# == 3 || $# == 4 ]] || die 'Use: install.sh backup-cleanup PRIVATE_S3_ENV PRIVATE_CATALOG [DIRECTORY]'
  task_backup_env="$2"
  task_backup_catalog="$3"
  task_root="${4:-$task_default_root}"
elif [[ "$task_action" == backup-schedule ]]; then
  [[ $# == 4 || $# == 5 ]] || die 'Use: install.sh backup-schedule PRIVATE_S3_ENV KEY_FILE "CALENDAR" [DIRECTORY]'
  task_backup_env="$2"
  task_backup_key="$3"
  task_backup_calendar="$4"
  task_root="${5:-$task_default_root}"
elif [[ "$task_action" == backup-schedule-local ]]; then
  [[ $# == 2 || $# == 3 ]] || die 'Use: install.sh backup-schedule-local "CALENDAR" [DIRECTORY]'
  task_backup_calendar="$2"
  task_root="${3:-$task_default_root}"
elif [[ "$task_action" == backup-fetch ]]; then
  [[ $# == 6 ]] || die 'Use: install.sh backup-fetch PRIVATE_S3_ENV KEY_FILE OBJECT_KEY SHA256 OUTPUT_ARCHIVE'
  task_backup_env="$2"
  task_backup_key="$3"
  task_backup_object="$4"
  task_backup_digest="$5"
  task_backup_destination="$6"
fi
[[ "$task_root" =~ ^/[a-zA-Z0-9.\ _/-]+$ && "$task_root" != / && "$task_root" != *'/../'* && "$task_root" != */.. && ! -L "$task_root" ]] || die 'Use an absolute installation path with letters, digits, spaces, dot, dash or underscore, without traversal or symlinks.'
if [[ -f "$task_root/installation.json" ]]; then
  task_backend="$(python3 -B -c 'import json,sys; print(json.load(open(sys.argv[1]+"/installation.json")).get("backend","portable"))' "$task_root" 2>/dev/null)" || die 'Invalid installation state; preserve data and inspect it privately.'
  if [[ "$task_backend" == existing-compose ]]; then
    task_existing_args=()
    case "$task_action" in
      media-s3) task_existing_args=(--config "$task_s3_file") ;;
      backup-external) task_existing_args=(--config "$task_backup_env" --key "$task_backup_key") ;;
    esac
    exec python3 -B "$task_root/existing_compose.py" --directory "$task_root" "$task_action" "${task_existing_args[@]}"
  fi
  [[ "$task_backend" == portable ]] || die 'Unsupported installation backend; no changes performed.'
fi

host_dependencies() {
  command -v apt-get >/dev/null || die 'Supported hosts: Ubuntu 24.04/26.04 or Debian 12/13.'
  if command -v curl >/dev/null && command -v openssl >/dev/null &&
     command -v python3 >/dev/null && command -v ip >/dev/null &&
     command -v swapon >/dev/null && command -v dig >/dev/null && [[ -s /etc/ssl/certs/ca-certificates.crt ]] &&
     python3 -c 'import yaml, rich' >/dev/null 2>&1; then
    return
  fi
  apt-get update -qq
  apt-get install -y --no-install-recommends ca-certificates curl openssl python3 python3-yaml python3-rich util-linux iproute2 dnsutils
}

docker_dependencies() {
  # Do not remove conflicting packages or existing installations automatically.
  if ! command -v docker >/dev/null; then
    . /etc/os-release
    [[ "$ID" == ubuntu || "$ID" == debian ]] || die 'Unsupported OS for automatic Docker installation.'
    case "$ID:$VERSION_ID" in ubuntu:24.04|ubuntu:26.04|debian:12|debian:13) ;; *) die 'Unsupported OS release.';; esac
    [[ "$(dpkg --print-architecture)" == amd64 ]] || die 'This reproducible image lock is tested for Linux amd64.'
    for task_package in docker.io docker-compose docker-compose-v2 podman-docker containerd runc; do
      if [[ "$(dpkg-query -W -f='${Status}' "$task_package" 2>/dev/null || true)" == 'install ok installed' ]]; then
        die "Conflicting installed package: $task_package. Follow Docker's official migration instructions."
      fi
    done
    install -d -m 0755 /etc/apt/keyrings
    curl --fail --silent --show-error --location "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/matrix-docker.asc
    chmod 0644 /etc/apt/keyrings/matrix-docker.asc
    cat > /etc/apt/sources.list.d/matrix-docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/$ID
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/matrix-docker.asc
EOF
    apt-get update -qq
    apt-get install -y --no-install-recommends docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  fi
  systemctl enable --now docker.service
  docker compose version >/dev/null || die 'Install the official docker-compose-plugin package.'
  docker info >/dev/null
  [[ "$(docker info --format '{{.Architecture}}')" == x86_64 ]] || die 'Linux amd64 Docker is required by this lock.'
}

storage_dependencies() {
  if ! python3 -c 'import boto3' >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y --no-install-recommends python3-boto3
  fi
}

backup_dependencies() {
  if ! python3 -c 'import cryptography' >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y --no-install-recommends python3-cryptography
  fi
}

case "$task_action" in
  backup-schedule|backup-schedule-local|backup-schedule-status|backup-schedule-disable)
    [[ -f "$task_root/installation.json" ]] || die 'Run init DOMAIN first.'
    if [[ "$task_action" == backup-schedule-status || "$task_action" == backup-schedule-disable ]]; then
      task_schedule_action=status
      [[ "$task_action" != backup-schedule-disable ]] || task_schedule_action=disable
      exec python3 "$task_root/backup_schedule.py" "$task_schedule_action" --directory "$task_root"
    fi
    task_schedule_mode=local
    task_schedule_args=()
    if [[ "$task_action" == backup-schedule ]]; then
      backup_dependencies
      storage_dependencies
      task_schedule_mode=external
      task_schedule_args=(--settings "$task_backup_env" --key-file "$task_backup_key")
    fi
    exec python3 "$task_root/backup_schedule.py" configure --directory "$task_root" --mode "$task_schedule_mode" --calendar "$task_backup_calendar" "${task_schedule_args[@]}"
    ;;
  vault-connect|vault-auth|vault-sync|vault-recover|vault-disconnect)
    [[ -f "$task_root/installation.json" ]] || die 'Run init DOMAIN first.'
    task_vault_operation="${task_action#vault-}"
    task_vault_args=()
    [[ "$task_action" != vault-connect && "$task_action" != vault-auth ]] || task_vault_args=(--settings "$task_vault_env")
    exec python3 "$task_root/vault_cache.py" "$task_vault_operation" --directory "$task_root" "${task_vault_args[@]}"
    ;;
  backup-key|backup-external|backup-fetch|backup-cleanup)
    backup_dependencies
    if [[ "$task_action" == backup-key ]]; then
      exec python3 "$task_source/external_backup.py" key --key-file "$task_backup_key"
    fi
    storage_dependencies
    if [[ "$task_action" == backup-fetch ]]; then
      exec python3 "$task_source/external_backup.py" fetch --settings "$task_backup_env" --key-file "$task_backup_key" --object-key "$task_backup_object" --sha256 "$task_backup_digest" --destination "$task_backup_destination"
    fi
    [[ -f "$task_root/installation.json" ]] || die 'Run init DOMAIN first.'
    if [[ "$task_action" == backup-cleanup ]]; then
      exec python3 "$task_root/external_backup.py" cleanup --directory "$task_root" --settings "$task_backup_env" --catalog "$task_backup_catalog"
    fi
    exec python3 "$task_root/external_backup.py" export --directory "$task_root" --settings "$task_backup_env" --key-file "$task_backup_key"
    ;;
  media-local|media-recover|media-s3)
    [[ -f "$task_root/installation.json" ]] || die 'Run init DOMAIN first.'
    if [[ "$task_action" == media-s3 ]]; then
      storage_dependencies
      exec python3 "$task_root/media.py" --directory "$task_root" --settings "$task_s3_file" s3
    fi
    task_media_operation=local
    [[ "$task_action" != media-recover ]] || task_media_operation=recover
    exec python3 "$task_root/media.py" --directory "$task_root" "$task_media_operation"
    ;;
  s3-credentials|s3-credentials-recover|s3-credentials-cleanup)
    [[ -f "$task_root/installation.json" ]] || die 'An existing portable S3 installation is required.'
    task_s3_credentials_operation=rotate
    task_s3_credentials_args=()
    if [[ "$task_action" == s3-credentials ]]; then
      task_s3_credentials_args=(--settings "$task_s3_file")
    elif [[ "$task_action" == s3-credentials-recover ]]; then
      task_s3_credentials_operation=recover
    else
      task_s3_credentials_operation=cleanup
    fi
    exec python3 "$task_root/s3_credentials.py" --directory "$task_root" "${task_s3_credentials_args[@]}" "$task_s3_credentials_operation"
    ;;
  database-password|database-password-recover)
    [[ -f "$task_root/installation.json" ]] || die 'An existing portable installation is required.'
    task_database_password_operation=rotate
    [[ "$task_action" != database-password-recover ]] || task_database_password_operation=recover
    exec python3 "$task_root/pg_credentials.py" --directory "$task_root" "$task_database_password_operation"
    ;;
  database-external|database-recover|database-abort)
    [[ -f "$task_root/installation.json" ]] || die 'An existing portable installation is required.'
    task_database_args=()
    [[ "$task_action" != database-external ]] || task_database_args=(--settings "$task_postgres_file")
    exec python3 "$task_root/database.py" "${task_action#database-}" --directory "$task_root" "${task_database_args[@]}"
    ;;
  restore|restore-local)
    host_dependencies
    docker_dependencies
    task_restore_args=()
    [[ "$task_action" != restore-local ]] || task_restore_args=(--local-postgres)
    exec python3 "$task_source/restore.py" --archive "$task_archive" --directory "$task_root" --sha256 "$task_digest" --public-ip "$task_public_ip" "${task_restore_args[@]}"
    ;;
  restore-activate|move-abort)
    [[ -f "$task_root/installation.json" ]] || die 'Restore into an empty target first.'
    task_command_args=()
    [[ "$task_action" != restore-activate ]] || task_command_args=(--register-command)
    exec python3 "$task_root/lifecycle.py" --directory "$task_root" "$task_action" "${task_command_args[@]}"
    ;;
  init)
    host_dependencies
    python3 "$task_source/command_wrapper.py" check --directory "$task_root"
    python3 "$task_source/generate.py" --directory "$task_root" --domain "$task_domain" --initialize-env "${task_storage_args[@]}"
    if [[ "$task_source" != "$task_root" ]]; then
      python3 "$task_source/update.py" seed --source "$task_source" --directory "$task_root"
    fi
    python3 "$task_root/command_wrapper.py" install --directory "$task_root"
    printf 'Initialized %s. Continue with sudo misbot ui; manual configuration: %s/settings/server.env, sudo misbot config validate, sudo misbot start\n' "$task_root" "$task_root"
    ;;
  update|update-recover)
    [[ -f "$task_root/installation.json" ]] || die 'Update requires an existing portable installation.'
    task_update_operation=apply
    [[ "$task_action" != update-recover ]] || task_update_operation=recover
    exec python3 "$task_source/update.py" "$task_update_operation" --source "$task_source" --directory "$task_root"
    ;;
  up|generate)
    [[ -f "$task_root/installation.json" ]] || die 'Run init DOMAIN first.'
    if [[ "$task_action" == up ]]; then
      python3 "$task_source/lifecycle.py" --directory "$task_root" up --check-up-only
      python3 "$task_root/command_wrapper.py" check --directory "$task_root" --keep-default
    fi
    host_dependencies
    if [[ "$task_action" == up ]]; then
      python3 "$task_root/preflight.py" --directory "$task_root"
      task_media_mode="$(python3 -c 'import sys; sys.path.insert(0,sys.argv[1]); import preflight; print(preflight.settings(sys.argv[1]).get("MEDIA_MODE","local"))' "$task_root")"
      [[ "$task_media_mode" != s3 ]] || storage_dependencies
    fi
    docker_dependencies
    task_command_args=()
    [[ "$task_action" != up || -n "${MATRIX_INSTALLER_UPDATE_STAGE:-}" ]] || task_command_args=(--register-command)
    exec python3 "$task_root/lifecycle.py" --directory "$task_root" "$task_action" "${task_command_args[@]}"
    ;;
  preflight)
    [[ -f "$task_root/installation.json" ]] || die 'Run init DOMAIN first.'
    exec python3 "$task_root/preflight.py" --directory "$task_root"
    ;;
  admin-password|admin-password-recover)
    [[ -f "$task_root/installation.json" ]] || die 'Run init DOMAIN first.'
    if [[ "$task_action" == admin-password ]]; then
      exec python3 "$task_root/credentials.py" --directory "$task_root" --password-file "$task_password_file" rotate
    fi
    exec python3 "$task_root/credentials.py" --directory "$task_root" recover
    ;;
  user-password|user-password-recover)
    [[ -f "$task_root/installation.json" ]] || die 'Run init DOMAIN first.'
    if [[ "$task_action" == user-password ]]; then
      exec python3 "$task_root/user_credentials.py" --directory "$task_root" --password-file "$task_password_file" reset
    fi
    exec python3 "$task_root/user_credentials.py" --directory "$task_root" recover
    ;;
  remove)
    exec python3 "$task_root/remove_server.py" --directory "$task_root"
    ;;
  check|backup|backup-recover|restore-check|move-export|status|stop|down)
    [[ -f "$task_root/installation.json" ]] || die 'Run init DOMAIN first.'
    task_command_args=()
    exec python3 "$task_root/lifecycle.py" --directory "$task_root" "$task_action" "${task_command_args[@]}"
    ;;
  *) die 'Unknown command. Run install.sh help.';;
esac
