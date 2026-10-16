#!/bin/bash
# Published as install.sh with bootstrap.py appended by ci/bootstrap.py.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 077

if [[ $# == 1 && ( $1 == --help || $1 == -h ) ]]; then
    echo 'Usage: sudo bash install.sh [--version 1.2.0]'
    exit 0
fi
if [[ $# != 0 && ! ( $# == 2 && $1 == --version && $2 =~ ^v?[0-9]+\.[0-9]+\.[0-9]+$ ) ]]; then
    echo 'Usage: sudo bash install.sh [--version 1.2.0]' >&2
    exit 2
fi
if [[ $EUID != 0 ]]; then
    echo 'Запустите загрузчик через sudo bash.' >&2
    exit 1
fi
if [[ ! -t 0 || ! -t 1 ]]; then
    echo 'Нужен интерактивный SSH-терминал. Сохраните install.sh в файл и запустите sudo bash install.sh.' >&2
    exit 1
fi
if [[ $(uname -s) != Linux || $(uname -m) != x86_64 ]]; then
    echo 'Поддерживается Linux amd64.' >&2
    exit 1
fi
# This is the distribution-owned operating system identity, not downloaded data.
. /etc/os-release
case "$ID:${VERSION_ID:-}" in
    ubuntu:24.04|ubuntu:26.04|debian:12|debian:13) ;;
    *) echo 'Поддерживаются Ubuntu 24.04/26.04 и Debian 12/13.' >&2; exit 1 ;;
esac
if [[ ! -x /usr/bin/python3 ]]; then
    echo 'Подготавливаем Python из репозиториев системы…'
    export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
    apt_options=(-o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o DPkg::Lock::Timeout=60)
    /usr/bin/timeout 600 /usr/bin/apt-get "${apt_options[@]}" update -qq
    /usr/bin/timeout 900 /usr/bin/apt-get "${apt_options[@]}" install -y --no-install-recommends python3
fi
# The generated Python program is passed with -c, leaving stdin attached to SSH.
