"""IPv4 media addresses suitable for TV playback URLs.

Discovery uses the kernel's UDP route to the configured TV; ``connect`` does
not send a datagram or require a running ADB service. No Internet probe or
machine-specific LAN address is used. Callers must provide a manual address
when no usable TV route exists.
"""
from __future__ import annotations

import ipaddress
import socket
from collections.abc import Mapping


_WILDCARDS = {'', '*', '0.0.0.0', '::', '[::]'}


def _usable_ipv4(value: str, *, allow_loopback: bool = False) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError(f'Media address {value!r} is not an IPv4 address') from exc
    if address.version != 4:
        raise ValueError(f'Media address {value!r} requires IPv4 for the media server')
    if address.is_loopback:
        if allow_loopback:
            return str(address)
        raise ValueError(f'Media address {value!r} is loopback and unreachable by the TV')
    if (address.is_unspecified or address.is_multicast or address.is_link_local
            or address.is_reserved or address.packed[0] == 0):
        raise ValueError(f'Media address {value!r} is unusable for TV playback URLs')
    return str(address)


def _ipv4_addresses(host: str, *, allow_loopback: bool = False) -> list[str]:
    """Resolve IPv4 only, preserving resolver order and rejecting unsafe answers."""
    if host in _WILDCARDS:
        raise ValueError(f'Media address {host!r} is a wildcard; set advertise_host')
    if host.lower().rstrip('.') == 'localhost':
        return [_usable_ipv4('127.0.0.1', allow_loopback=allow_loopback)]
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return [_usable_ipv4(host, allow_loopback=allow_loopback)]
    # Accept a hostname, not a URL, host:port pair, or bracketed IPv6 literal.
    if any(char in host for char in '/:@[]?#') or any(char.isspace() for char in host):
        raise ValueError(f'Media host {host!r} must be an IPv4 address or hostname')
    try:
        answers = socket.getaddrinfo(host, 9, family=socket.AF_INET,
                                     type=socket.SOCK_DGRAM)
    except OSError as exc:
        raise ValueError(f'Cannot resolve media host {host!r} to IPv4; set advertise_host') from exc
    addresses = []
    for family, _, _, _, endpoint in answers:
        if family != socket.AF_INET:
            continue
        try:
            address = _usable_ipv4(endpoint[0], allow_loopback=allow_loopback)
        except ValueError:
            continue
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise ValueError(f'Media host {host!r} has no usable IPv4 address; set advertise_host')
    return addresses


def _tv_hosts(cfg) -> list[str]:
    if not isinstance(cfg, Mapping):
        return []
    adb = cfg.get('adb') or {}
    if not isinstance(adb, Mapping):
        return []
    hosts = []
    host = str(adb.get('host') or '').strip()
    if host:
        hosts.append(host)
    serial = str(adb.get('serial') or '').strip()
    # USB serials/emulator identifiers do not identify a network route.
    name, separator, port = serial.rpartition(':')
    if separator and name and port.isdecimal() and 0 < int(port) <= 65535:
        if name not in hosts:
            hosts.append(name)
    return hosts


def resolve_media_host(cfg=None, host='', advertise_host='', allow_loopback=False) -> str:
    """Return a usable IPv4 host for media binding/TV playback URLs.

    Precedence is an explicit ``advertise_host``, then a non-wildcard ``host``,
    then the local IPv4 address selected by a UDP route to ``cfg['adb']['host']``
    or the hostname in ``cfg['adb']['serial']`` (``host:port``). Pass media
    configuration values as explicit arguments; this function never loads
    configuration or reads the network when a literal manual address wins.

    ``allow_loopback=True`` permits *explicit* localhost/127.x addresses for
    internal use. It never turns failed discovery into a loopback fallback.
    Wildcard bind hosts trigger discovery; wildcard advertised hosts are errors.
    IPv6 is rejected because the current media server binds IPv4 sockets.
    Failure raises ``ValueError`` with guidance to configure ``advertise_host``.
    """
    advertised = str(advertise_host or '').strip()
    bound = str(host or '').strip()
    if advertised:
        return _ipv4_addresses(advertised, allow_loopback=allow_loopback)[0]
    if bound not in _WILDCARDS:
        return _ipv4_addresses(bound, allow_loopback=allow_loopback)[0]

    last_error = None
    for target in _tv_hosts(cfg):
        try:
            destinations = _ipv4_addresses(target)
        except ValueError as exc:
            last_error = exc
            continue
        for destination in destinations:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                    probe.connect((destination, 9))
                    return _usable_ipv4(probe.getsockname()[0])
            except (OSError, ValueError) as exc:
                last_error = exc
    message = ('Cannot discover a TV-reachable media IPv4 address from adb.host '
               'or adb.serial; set media.advertise_host to a reachable IPv4 '
               'address or hostname')
    if last_error is not None:
        raise ValueError(f'{message}: {last_error}') from last_error
    raise ValueError(message)
