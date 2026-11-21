"""Collectors against fixtures and a scripted platform."""

import json

from bandwatch import platform as plat
from bandwatch.collectors import aria2 as aria2mod
from bandwatch.collectors import extras, identity, perdevice, wan
from bandwatch.config import SecretStore
from conftest import net_dev


class Sink:
    def __init__(self):
        self.rows = []

    def __call__(self, series, key, end, dt, rx, tx, rate_ok=True, est=False, count=True):
        self.rows.append((series, key, end, dt, rx, tx, rate_ok))

    def total(self, series, key):
        return sum(r[4] for r in self.rows if r[0] == series and r[1] == key), sum(r[5] for r in self.rows if r[0] == series and r[1] == key)


# -- parsers --------------------------------------------------------------------


def test_parse_routes(fixture_text):
    assert plat.parse_route_v4(fixture_text("proc_net_route.txt")) == [("wwan0", 0), ("eth1", 20)]
    assert plat.parse_route_v6(fixture_text("proc_net_ipv6_route.txt")) == [("wwan0", 1024)]


def test_parse_arp_and_leases(fixture_text):
    arp = plat.parse_arp(fixture_text("proc_net_arp.txt"))
    assert [a["mac"] for a in arp] == ["00:1a:2b:00:00:11", "00:1a:2b:00:00:12", "00:1a:2b:00:00:99"]
    assert arp[1]["complete"] is False
    leases = plat.parse_dhcp_leases(fixture_text("dhcp.leases"))
    assert len(leases) == 2 and leases[1]["hostname"] is None


def test_parse_brforward_struct():
    import struct

    data = struct.pack("=6sBBIBBH", bytes.fromhex("001a2b000011"), 3, 0, 1234, 0, 0, 0)
    data += struct.pack("=6sBBIBBH", bytes.fromhex("aabbccddeeff"), 1, 1, 0, 1, 0, 0)
    out = plat.parse_brforward(data)
    assert out[0] == ("00:1a:2b:00:00:11", 3, False, 12.34)
    assert out[1][1] == 257 and out[1][2] is True


def test_parse_net_dev_and_cpu():
    nd = plat.parse_net_dev(net_dev({"wwan0": (100, 50), "lo": (1, 1)}))
    assert nd["wwan0"][:2] == (100, 50)
    assert plat.parse_proc_stat_cpu("cpu  10 0 10 80 0 0 0 0 0 0\n") == (20, 100)


# -- WAN detection --------------------------------------------------------------


def test_ubus_detection_dedupes_and_ignores_tunnels(fixture_text):
    dump = json.loads(fixture_text("ubus_network_dump.json"))
    types = {"pppoe-wan": 512, "6in4-henet": 776, "wg0": 65534}
    cands = wan.detect_from_ubus(dump, lambda d: types.get(d, 1))
    devs = {c[0]: c for c in cands}
    assert set(devs) == {"pppoe-wan", "6in4-henet"}  # wg0 is down
    assert devs["pppoe-wan"][3] == {"ipv4", "ipv6"}
    assert devs["pppoe-wan"][1] == "wan+wan6"
    chosen = wan.choose(cands)
    assert [c[0] for c in chosen] == ["pppoe-wan"]  # tunnel would double count


def test_only_tunnel_kept_when_nothing_else():
    cands = [("wg0", "vpn", "wireguard", {"ipv4"}, True)]
    assert wan.choose(cands) == cands


def _wan_platform(fake, counters):
    fake.files["/proc/net/dev"] = lambda: net_dev(counters)
    fake.files["/proc/net/route"] = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\nwwan0\t00000000\t0102000A\t0003\t0\t0\t0\t00000000\n"
    fake.files["/proc/net/ipv6_route"] = ""
    fake.files["/sys/class/net/wwan0/ifindex"] = "7\n"
    fake.files["/sys/class/net/wwan0/type"] = "519\n"
    fake.files["/proc/sys/kernel/random/boot_id"] = "boot-a\n"


def test_wan_sampling_reset_and_reconnect(fake, settings):
    counters = {"wwan0": (1_000_000, 200_000), "br-lan": (5, 5)}
    _wan_platform(fake, counters)
    col = wan.WanCollector(fake, settings)
    sink = Sink()
    assert col.sample(fake.mono, sink) == (None, None)  # baseline
    assert col.source == "routes" and list(col.ifaces) == ["wwan0"]
    fake.advance(2)
    counters["wwan0"] = (1_400_000, 260_000)
    rx, tx = col.sample(fake.mono, sink)
    assert (rx, tx) == (200_000, 30_000)
    # Interface disappears (cellular reconnect): measured zero, WAN down event
    del counters["wwan0"]
    fake.advance(2)
    assert col.sample(fake.mono, sink) == (0.0, 0.0)
    assert any(e[1] == "wan_down" for e in col.events)
    # Comes back with a new ifindex and fresh counters
    counters["wwan0"] = (3000, 1000)
    fake.files["/sys/class/net/wwan0/ifindex"] = "9\n"
    fake.advance(2)
    col.sample(fake.mono, sink)  # baseline after re-creation (tracker was reset)
    fake.advance(2)
    counters["wwan0"] = (5000, 1500)
    col.sample(fake.mono, sink)
    assert sink.total("wan", "*") == (400_000 + 2000, 60_000 + 500)
    assert sink.total("wan", "wwan0") == (400_000 + 2000, 60_000 + 500)


def test_wan_override_and_counter_reset_without_reconnect(fake, settings):
    counters = {"eth1": (10_000_000_000, 1_000_000_000)}
    fake.files["/proc/net/dev"] = lambda: net_dev(counters)
    fake.files["/sys/class/net/eth1/ifindex"] = "3\n"
    settings.values["wan.interfaces"] = ["eth1"]
    col = wan.WanCollector(fake, settings)
    sink = Sink()
    col.sample(fake.mono, sink)
    assert col.source == "override"
    fake.advance(2)
    counters["eth1"] = (12_345, 678)  # counter reset (e.g. driver reload)
    col.sample(fake.mono, sink)
    assert sink.total("wan", "*") == (12_345, 678)


def test_wan_no_default_route_reports_unavailable(fake, settings):
    fake.files["/proc/net/dev"] = net_dev({"br-lan": (1, 1)})
    fake.files["/proc/net/route"] = "Iface\tDestination\n"
    col = wan.WanCollector(fake, settings)
    assert col.sample(fake.mono, Sink()) == (None, None)
    assert col.status.state == "unavailable"


# -- Bandix -----------------------------------------------------------------------


def _bandix(fake, payload_fn):
    fake.http["http://127.0.0.1:8686/api/system/info"] = (200, {"status": "success", "data": {"version": "0.12.12"}})
    fake.http["http://127.0.0.1:8686/api/traffic/devices"] = lambda url, body: (200, payload_fn())


def test_bandix_provider_parses_documented_schema(fake, settings, fixture_text):
    doc = json.loads(fixture_text("bandix_devices_0.12.json"))
    _bandix(fake, lambda: doc)
    prov = perdevice.BandixProvider(fake, settings)
    sink = Sink()
    snaps = prov.poll(fake.mono, sink)
    assert prov.version == "0.12.12" and prov.status.state == "ok"
    assert set(snaps) == {"00:1a:2b:00:00:11", "0a:3c:41:00:00:13"}  # MAC normalised, invalid dropped
    tv = snaps["00:1a:2b:00:00:11"]
    assert tv.ip6 == "fd00::20" and tv.down == 100000 and tv.up == 3000 and tv.freq is None
    assert snaps["0a:3c:41:00:00:13"].host is None and snaps["0a:3c:41:00:00:13"].freq == 5180
    # second poll: deltas recorded, WAN and LAN kept separate
    d = doc["data"]["d"][0]
    d["w_rx_b"] += 30000
    d["w_tx_b"] += 3000
    d["l_rx_b"] += 7000
    fake.advance(3)
    prov.poll(fake.mono, sink)
    assert sink.total("dev", "00:1a:2b:00:00:11") == (30000, 3000)
    assert sink.total("devl", "00:1a:2b:00:00:11") == (7000, 0)
    assert sink.total("dev", "*") == (30000, 3000)


def test_bandix_direction_swap_setting(fake, settings, fixture_text):
    doc = json.loads(fixture_text("bandix_devices_0.12.json"))
    _bandix(fake, lambda: doc)
    settings.values["devices.bandix_direction"] = "tx_is_download"
    prov = perdevice.BandixProvider(fake, settings)
    snaps = prov.poll(fake.mono, Sink())
    assert snaps["00:1a:2b:00:00:11"].down == 3000


def test_bandix_restart_does_not_double_count(fake, settings, fixture_text):
    doc = json.loads(fixture_text("bandix_devices_0.12.json"))
    _bandix(fake, lambda: doc)
    prov = perdevice.BandixProvider(fake, settings)
    sink = Sink()
    prov.poll(fake.mono, sink)
    d = doc["data"]["d"][0]
    d["w_rx_b"], d["w_tx_b"] = 500, 50  # Bandix restarted: counters from zero
    fake.advance(3)
    prov.poll(fake.mono, sink)
    assert sink.total("dev", "00:1a:2b:00:00:11") == (500, 50)


def test_bandix_unreachable_keeps_wan_only(fake, settings):
    prov = perdevice.BandixProvider(fake, settings)
    assert prov.poll(fake.mono, Sink()) is None
    assert prov.status.state == "unavailable"
    # backoff: no immediate retry
    n = len(fake.http_calls)
    prov.poll(fake.mono + 1, Sink())
    assert len(fake.http_calls) == n


def test_bandix_never_calls_write_endpoints(fake, settings, fixture_text):
    doc = json.loads(fixture_text("bandix_devices_0.12.json"))
    _bandix(fake, lambda: doc)
    prov = perdevice.BandixProvider(fake, settings)
    for _ in range(3):
        prov.poll(fake.mono, Sink())
        fake.advance(3)
    assert all(body is None for _url, body in fake.http_calls)
    assert {u.split("8686")[1] for u, _ in fake.http_calls} <= {"/api/system/info", "/api/traffic/devices"}


def test_auto_provider_falls_back_to_nlbwmon(fake, settings):
    fake.bins["nlbw"] = "/usr/sbin/nlbw"
    state = {"rx": 1000}
    fake.commands[("nlbw", "-c", "json", "-g", "mac")] = lambda: json.dumps({"columns": ["family", "mac", "conns", "rx_bytes", "rx_pkts", "tx_bytes", "tx_pkts"], "data": [[4, "AA:BB:CC:00:00:01", 3, state["rx"], 1, 10, 1]]})
    prov = perdevice.AutoProvider(fake, settings)
    sink = Sink()
    prov.poll(fake.mono, sink)
    assert prov.active is prov.nlbw
    state["rx"] = 5000
    fake.advance(30)
    prov.poll(fake.mono, sink)
    assert sink.total("dev", "aa:bb:cc:00:00:01") == (4000, 0)
    assert prov.status.state == "degraded"


def test_no_provider_message(fake, settings):
    prov = perdevice.AutoProvider(fake, settings)
    assert prov.poll(fake.mono, Sink()) is None
    assert "WAN monitoring continues" in prov.status.message


# -- aria2 --------------------------------------------------------------------------


class Aria2Fake:
    def __init__(self):
        self.session = "s1"
        self.tasks = {"a1": {"gid": "a1", "status": "active", "totalLength": "1000000", "completedLength": "400000", "uploadLength": "0", "downloadSpeed": "1000", "uploadSpeed": "0", "files": [{"path": "/mnt/d/file.iso"}]}}
        self.secret = None
        self.calls = []

    def __call__(self, url, body):
        results = []
        for call in body["params"][0]:
            m, params = call["methodName"], call["params"]
            self.calls.append(m)
            if self.secret and (not params or params[0] != "token:" + self.secret):
                results.append({"faultCode": 1, "faultString": "Unauthorized"})
                continue
            if m == "aria2.getVersion":
                results.append([{"version": "1.37.0"}])
            elif m == "aria2.getSessionInfo":
                results.append([{"sessionId": self.session}])
            elif m == "aria2.getGlobalStat":
                results.append([{"downloadSpeed": "1000", "uploadSpeed": "0", "numActive": "1", "numWaiting": "0", "numStoppedTotal": "0"}])
            elif m == "aria2.tellActive":
                results.append([[t for t in self.tasks.values() if t["status"] == "active"]])
            elif m == "aria2.tellWaiting":
                results.append([[t for t in self.tasks.values() if t["status"] in ("waiting", "paused")]])
            elif m == "aria2.tellStopped":
                results.append([[t for t in self.tasks.values() if t["status"] in ("complete", "error", "removed")]])
        return 200, {"jsonrpc": "2.0", "id": "bw", "result": results}


def test_aria2_does_not_count_preexisting_and_handles_restart(fake, settings, tmp_path):
    rpc = Aria2Fake()
    fake.http["http://127.0.0.1:6800/jsonrpc"] = rpc
    col = aria2mod.Aria2Collector(fake, settings, SecretStore(str(tmp_path / "s.json")))
    sink = Sink()
    live = col.poll(fake.mono, fake.wall, sink)
    assert live["active"] == 1 and sink.total("aria2", "*") == (0, 0)  # 400 kB pre-existing not counted
    rpc.tasks["a1"]["completedLength"] = "700000"
    fake.advance(3)
    col.poll(fake.mono, fake.wall, sink)
    assert sink.total("aria2", "*") == (300000, 0)
    # completes between polls
    rpc.tasks["a1"].update(status="complete", completedLength="1000000")
    fake.advance(3)
    col.poll(fake.mono, fake.wall, sink)
    assert sink.total("aria2", "*") == (600000, 0)
    assert any(e[1] == "aria2_complete" for e in col.events)
    # aria2 restarts: same task reappears with a new session; nothing counted
    rpc.session = "s2"
    rpc.tasks = {"b1": {"gid": "b1", "status": "active", "totalLength": "5000000", "completedLength": "2000000", "uploadLength": "0", "files": []}}
    fake.advance(3)
    col.poll(fake.mono, fake.wall, sink)
    assert sink.total("aria2", "*") == (600000, 0)
    assert any(e[1] == "aria2_restart" for e in col.events)
    # only read-only methods were used
    assert set(rpc.calls) <= {"aria2.getVersion", "aria2.getSessionInfo", "aria2.getGlobalStat", "aria2.tellActive", "aria2.tellWaiting", "aria2.tellStopped"}


def test_aria2_secret_sent_and_error_reported(fake, settings, tmp_path):
    rpc = Aria2Fake()
    rpc.secret = "pw"
    fake.http["http://127.0.0.1:6800/jsonrpc"] = rpc
    secrets = SecretStore(str(tmp_path / "s.json"))
    col = aria2mod.Aria2Collector(fake, settings, secrets)
    assert col.poll(fake.mono, fake.wall, Sink()) is None
    assert col.status.state == "error" and "secret" in col.status.message
    assert "pw" not in col.status.message
    secrets.set("aria2_secret", "pw")
    fake.advance(400)
    assert col.poll(fake.mono, fake.wall, Sink())["active"] == 1


def test_aria2_outage_keeps_baseline(fake, settings, tmp_path):
    rpc = Aria2Fake()
    state = {"down": False}

    def handler(url, body):
        if state["down"]:
            return OSError("refused")
        return rpc(url, body)

    fake.http["http://127.0.0.1:6800/jsonrpc"] = handler
    col = aria2mod.Aria2Collector(fake, settings, SecretStore(str(tmp_path / "s.json")))
    sink = Sink()
    col.poll(fake.mono, fake.wall, sink)
    state["down"] = True
    fake.advance(3)
    assert col.poll(fake.mono, fake.wall, sink) is None
    state["down"] = False
    rpc.tasks["a1"]["completedLength"] = "900000"
    fake.advance(60)
    col.poll(fake.mono, fake.wall, sink)
    assert sink.total("aria2", "*") == (500000, 0)  # progress during outage counted once
    assert sink.rows[-1][6] is False  # but not used for peak statistics


def test_aria2_disabled(fake, settings, tmp_path):
    settings.values["aria2.mode"] = "off"
    col = aria2mod.Aria2Collector(fake, settings, SecretStore(str(tmp_path / "s.json")))
    assert col.poll(fake.mono, fake.wall, Sink()) is None and col.status.state == "disabled"
    assert not fake.http_calls


# -- identity / system / vnstat -------------------------------------------------------


def test_identity_merges_sources(fake, settings, fixture_text):
    fake.files["/tmp/dhcp.leases"] = fixture_text("dhcp.leases")
    fake.files["/proc/net/arp"] = fixture_text("proc_net_arp.txt")
    fake.files["/etc/config/dhcp"] = "config host\n\toption name 'tv-static'\n\toption mac '00:1A:2B:00:00:11'\n"
    col = identity.IdentityCollector(fake, settings)
    snap = perdevice.DeviceSnapshot("0a:3c:41:00:00:13")
    snap.conn = "wifi"
    snap.freq = 5180
    devs = col.poll(fake.mono, {"0a:3c:41:00:00:13": snap})
    assert devs["00:1a:2b:00:00:11"].static_name == "tv-static"
    assert devs["00:1a:2b:00:00:11"].hostname == "living-room-tv"
    assert "00:1a:2b:00:00:99" not in devs  # neighbour on the WAN side is not a LAN device
    phone = devs["0a:3c:41:00:00:13"]
    assert phone.conn == "wifi" and phone.band == "5 GHz"
    assert col.presence(phone, 0, 180) != "online"  # Wi-Fi type alone does not mean associated
    assert identity.is_randomized("0a:3c:41:00:00:13") and not identity.is_randomized("00:1a:2b:00:00:11")


def test_vnstat_parse(fixture_text):
    out = extras.parse_vnstat(json.loads(fixture_text("vnstat.json")))
    assert out["version"] == "2.13"
    assert out["interfaces"]["wwan0"]["days"][1] == {"y": 2026, "m": 11, "d": 21, "rx": 3000, "tx": 400}


def test_offload_notes(fake):
    fake.files["/etc/config/firewall"] = "config defaults\n\toption flow_offloading '1'\n\toption flow_offloading_hw '1'\n"
    notes = extras.offload_notes(fake)
    assert len(notes) == 2
