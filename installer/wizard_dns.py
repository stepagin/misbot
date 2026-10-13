"""Read-only per-host DNS status for the initial installation wizard."""

from dataclasses import dataclass
import queue
import socket
import threading

import configuration_schema
import preflight


@dataclass(frozen=True)
class HostStatus:
    host: str
    expected: str
    addresses: tuple[str, ...] = ()
    ipv6: tuple[str, ...] = ()
    query_failed: bool = False
    external: bool = False

    @property
    def passed(self):
        if self.query_failed:
            return False
        if self.external:
            return (
                self.addresses == (self.expected,)
                if self.expected
                else bool(self.addresses or self.ipv6)
            )
        return self.addresses == (self.expected,) and not self.ipv6

    @property
    def message(self):
        if self.query_failed:
            return 'DNS не отвечает; проверка будет повторена'
        if self.external and not self.expected:
            if self.passed:
                return 'готово: внешний MAS, адреса ' + ', '.join(self.addresses + self.ipv6)
            return 'создать A или AAAA на адрес внешнего сервера MAS'
        if self.passed:
            return 'готово: A = ' + self.expected
        actions = []
        if not self.addresses:
            actions.append('создать A = ' + self.expected)
        elif self.addresses != (self.expected,):
            actions.append('исправить A: ' + ', '.join(self.addresses) + ' → ' + self.expected)
        if self.ipv6 and not self.external:
            actions.append('удалить AAAA')
        return '; '.join(actions)


def probe(host, expected, resolver=None, external=False):
    resolver = resolver or (lambda host, family: preflight.resolve(host, family, strict=True))
    try:
        addresses = tuple(sorted(resolver(host, socket.AF_INET)))
        ipv6 = tuple(sorted(resolver(host, socket.AF_INET6)))
    except (OSError, RuntimeError):
        return HostStatus(host, expected, query_failed=True, external=external)
    return HostStatus(host, expected, addresses, ipv6, external=external)


def scan(values, expected, on_status=None, resolver=None):
    """A bounded concurrent scan; daemon queries never block wizard cancellation."""
    hosts = configuration_schema.public_hosts(values)
    external_host = (
        configuration_schema.mas_host(values) if values.get('MAS_MODE') == 'external' else None
    )
    if external_host:
        hosts.append(external_host)
    results = queue.Queue()

    def check(host):
        try:
            external = host == external_host
            host_ip = values.get('MAS_PUBLIC_IP', '') if external else expected
            result = probe(host, host_ip, resolver, external=external)
        except Exception:
            # Never include resolver responses or exception arguments in diagnostics.
            result = HostStatus(host, expected, query_failed=True, external=host == external_host)
        results.put(result)

    for host in hosts:
        threading.Thread(target=check, args=(host,), daemon=True).start()
    statuses = {}
    for _ in hosts:
        status = results.get()
        statuses[status.host] = status
        if on_status is not None:
            on_status(status)
    return [statuses[host] for host in hosts]
