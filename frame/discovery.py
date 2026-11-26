"""Finding Frame TVs on the network, and which of them Frame uses.

The worker sweeps the hub's own /24 (plus any subnets in the ``scan_subnets``
setting, for TVs on another VLAN) for Samsung's ``:8001/api/v2/`` and keeps
every Frame it finds in the TV table, keyed by the TV's own id so a new IP
address doesn't make it a new TV. Admins pick the TVs to use on the hub's
Settings page (``tvs``); until they do, Frame uses every Frame it finds.
"""
import ipaddress
import logging
import socket
from concurrent.futures import ThreadPoolExecutor

from bdh_sdk.plugins import as_list

from . import service, tv as tvlib
from .models import TV

log = logging.getLogger(__name__)

PORT = 8001
CONNECT_TIMEOUT = 0.5
WORKERS = 64
#: Largest subnet Frame sweeps (a /22 is ~1000 addresses).
MIN_PREFIX = 22
#: Placeholder host for a TV whose address another TV now has (until it's found again).
MOVED = "moved-"


def local_subnet():
    """The /24 of the hub's main address (the container shares the host's network)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # no packet is sent; this just picks the route
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    net = ipaddress.ip_network(f"{ip}/24", strict=False)
    return None if net.is_loopback else net


def parse_subnets(raw):
    """``scan_subnets`` text ("192.168.11.0/24, 10.0.5.0/24") -> networks; bad ones are skipped."""
    out = []
    for part in str(raw or "").replace(",", " ").split():
        try:
            net = ipaddress.ip_network(part, strict=False)
        except ValueError:
            log.warning("Ignoring %r in scan_subnets: not a subnet", part)
            continue
        if net.version != 4 or net.prefixlen < MIN_PREFIX:
            log.warning("Ignoring %s in scan_subnets: use IPv4 /%d or smaller", net, MIN_PREFIX)
            continue
        out.append(net)
    return out


def subnets():
    nets = [n for n in [local_subnet()] if n is not None]
    for n in parse_subnets(service.setting("scan_subnets", "")):
        if n not in nets:
            nets.append(n)
    return nets


def _open(host):
    try:
        with socket.create_connection((host, PORT), timeout=CONNECT_TIMEOUT):
            return True
    except OSError:
        return False


def _probe(host):
    if not _open(host):
        return None
    try:
        d = tvlib.describe(host)
    except tvlib.Offline:
        return None
    return dict(d, host=host) if d["frame"] and d.get("uid") else None


def scan(nets):
    """Every Frame TV answering in ``nets`` (plus the TVs Frame already knows)."""
    known = [h for h in TV.objects.values_list("host", flat=True) if not h.startswith(MOVED)]
    hosts = list(dict.fromkeys([str(ip) for n in nets for ip in n.hosts()] + known))
    with ThreadPoolExecutor(WORKERS, thread_name_prefix="frame-scan") as pool:
        return [d for d in pool.map(_probe, hosts) if d]


def _vacate(host):
    """Another TV answers at ``host`` now: the one Frame had there has moved on (host is unique)."""
    for t in TV.objects.filter(host=host):
        t.host = f"{MOVED}{t.id}"
        t.online = False
        t.save(update_fields=["host", "online"])


def remember(found):
    """Add new TVs and follow known ones to a new address. Returns the TVs added."""
    added = []
    for d in found:
        t = TV.objects.filter(uid=d["uid"]).first() or TV.objects.filter(host=d["host"], uid="").first()
        if t is None:
            _vacate(d["host"])
            added.append(TV.objects.create(uid=d["uid"], host=d["host"], name=d["name"][:80], model=d["model"]))
            log.info("Found a Frame TV: %s at %s", d["name"], d["host"])
            continue
        changed = {}
        if t.uid != d["uid"]:
            changed["uid"] = d["uid"]
        if t.host != d["host"]:
            _vacate(d["host"])
            log.info("%s moved from %s to %s", t, t.host, d["host"])
            changed["host"] = d["host"]
        if d["model"] and t.model != d["model"]:
            changed["model"] = d["model"]
        if changed:
            for k, v in changed.items():
                setattr(t, k, v)
            t.save(update_fields=list(changed))
    return added


def discover():
    nets = subnets()
    found = scan(nets)
    log.info("Looked for Frame TVs on %s: found %d", ", ".join(map(str, nets)) or "nothing", len(found))
    return remember(found)


# --- which TVs Frame uses --------------------------------------------------------------

def key(t):
    return t.uid or f"host:{t.host}"


def skipped():
    """Keys of the TVs an admin ticked under "Leave these TVs alone"."""
    try:
        return set(as_list(service.setting("skip_tvs", None)))
    except ValueError:
        return set()


def enabled(t, skip=None):
    return key(t) not in (skipped() if skip is None else skip)


def enabled_tvs():
    skip = skipped()
    return [t for t in TV.objects.all() if enabled(t, skip)]


def tv_choices():
    """The Settings page's "Leave these TVs alone" checkboxes: every Frame found so far."""
    out = []
    for t in TV.objects.all():
        note = "" if t.token else " \u00b7 not paired yet"
        out.append((key(t), f"{t.name} \u00b7 {t.host}{note}"))
    return out
