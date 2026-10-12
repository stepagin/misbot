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

    @property
    def passed(self):
        return not self.query_failed and self.addresses == (self.expected,) and not self.ipv6

    @property
    def message(self):
        if self.query_failed:
            return 'DNS не отвечает; проверка будет повторена'
        if self.passed:
            return 'готово: A = ' + self.expected
        actions = []
        if not self.addresses:
            actions.append('создать A = ' + self.expected)
        elif self.addresses != (self.expected,):
            actions.append('исправить A: ' + ', '.join(self.addresses) + ' → ' + self.expected)
        if self.ipv6:
            actions.append('удалить AAAA')
        return '; '.join(actions)


def probe(host, expected, resolver=None):
    resolver = resolver or (lambda host, family: preflight.resolve(host, family, strict=True))
    try:
        addresses = tuple(sorted(resolver(host, socket.AF_INET)))
        ipv6 = tuple(sorted(resolver(host, socket.AF_INET6)))
    except (OSError, RuntimeError):
        return HostStatus(host, expected, query_failed=True)
    return HostStatus(host, expected, addresses, ipv6)


def scan(values, expected, on_status=None, resolver=None):
    """A bounded concurrent scan; daemon queries never block wizard cancellation."""
    hosts = configuration_schema.public_hosts(values)
    results = queue.Queue()

    def check(host):
        try:
            result = probe(host, expected, resolver)
        except Exception:
            # Never include resolver responses or exception arguments in diagnostics.
            result = HostStatus(host, expected, query_failed=True)
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
