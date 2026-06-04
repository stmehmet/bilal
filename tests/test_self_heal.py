"""Tests for speaker self-healing: name-based re-resolution when IPs change.

A speaker's friendly name is stable; its DHCP-assigned IP is not.  These tests
cover the machinery that keeps playback working when an address drifts:
identity-verified direct-connect, mDNS-by-name, the unicast subnet scan
fallback, and the carry-forward persistence that updates only host/port.
"""

from unittest.mock import MagicMock

import pytest

import discovery
import adhan_scheduler as sched


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeCastInfo:
    def __init__(self, friendly_name, host, port=8009, cast_type="cast", uuid=None):
        self.friendly_name = friendly_name
        self.host = host
        self.port = port
        self.cast_type = cast_type
        self.model_name = "FakeNest"
        self.uuid = uuid


class FakeCast:
    def __init__(self, friendly_name, host, port=8009, cast_type="cast", uuid=None):
        self.cast_info = FakeCastInfo(friendly_name, host, port, cast_type, uuid)
        self.waited = False
        self.disconnected = False

    def wait(self, timeout=None):
        self.waited = True

    def disconnect(self, timeout=None):
        self.disconnected = True


def fake_get_listed(by_host=None, by_name=None):
    """Build a stand-in for pychromecast.get_listed_chromecasts.

    ``by_host`` maps an IP -> the FakeCast currently answering there (used by
    direct-connect / known_hosts calls).  ``by_name`` maps friendly name ->
    FakeCast reachable via mDNS (used by friendly_names calls).
    """
    by_host = by_host or {}
    by_name = by_name or {}

    def _fake(friendly_names=None, uuids=None, tries=None, retry_wait=None,
              timeout=None, discovery_timeout=None, zeroconf_instance=None,
              known_hosts=None):
        browser = MagicMock()
        if known_hosts:
            return [by_host[h] for h in known_hosts if h in by_host], browser
        if friendly_names:
            return [by_name[n] for n in friendly_names if n in by_name], browser
        return list(by_name.values()), browser

    return _fake


def fake_get_chromecasts(casts):
    """Build a stand-in for pychromecast.get_chromecasts (the full browse).

    Returns every cast on the "network" plus a browser, exactly like the real
    call — the resolver is responsible for filtering to the wanted devices and
    disconnecting the rest.
    """
    def _fake(timeout=None):
        return list(casts), MagicMock()
    return _fake


@pytest.fixture
def patch_listed(monkeypatch):
    def _apply(by_host=None, by_name=None):
        monkeypatch.setattr(
            discovery.pychromecast, "get_listed_chromecasts",
            fake_get_listed(by_host, by_name), raising=False,
        )
    return _apply


@pytest.fixture
def patch_browse(monkeypatch):
    def _apply(casts):
        monkeypatch.setattr(
            discovery.pychromecast, "get_chromecasts",
            fake_get_chromecasts(casts), raising=False,
        )
    return _apply


# ---------------------------------------------------------------------------
# connect_by_host — identity verification
# ---------------------------------------------------------------------------
class TestConnectByHostIdentity:
    def test_matching_name_returns_device(self, patch_listed):
        office = FakeCast("Office", "10.0.0.5")
        patch_listed(by_host={"10.0.0.5": office})
        cc = discovery.connect_by_host("10.0.0.5", expected_name="Office")
        assert cc is office
        assert cc.waited

    def test_wrong_device_at_stale_ip_is_rejected(self, patch_listed):
        # DHCP gave Office's old IP to a different cast device.
        kitchen = FakeCast("Kitchen", "10.0.0.5")
        patch_listed(by_host={"10.0.0.5": kitchen})
        cc = discovery.connect_by_host("10.0.0.5", expected_name="Office")
        assert cc is None
        assert kitchen.disconnected  # we let go of the wrong device

    def test_dead_ip_returns_none(self, patch_listed):
        patch_listed(by_host={})
        assert discovery.connect_by_host("10.0.0.5", expected_name="Office") is None

    def test_no_expected_name_accepts_any(self, patch_listed):
        kitchen = FakeCast("Kitchen", "10.0.0.5")
        patch_listed(by_host={"10.0.0.5": kitchen})
        assert discovery.connect_by_host("10.0.0.5") is kitchen

    def test_matching_uuid_returns_device(self, patch_listed):
        office = FakeCast("Office", "10.0.0.5", uuid="uuid-office")
        patch_listed(by_host={"10.0.0.5": office})
        cc = discovery.connect_by_host("10.0.0.5", expected_uuid="uuid-office")
        assert cc is office

    def test_uuid_wins_over_name_match(self, patch_listed):
        # Same friendly name, but a *different* physical device (UUID changed) —
        # e.g. the user swapped the unit. Pinned-by-device must reject it.
        imposter = FakeCast("Office", "10.0.0.5", uuid="uuid-new")
        patch_listed(by_host={"10.0.0.5": imposter})
        cc = discovery.connect_by_host(
            "10.0.0.5", expected_name="Office", expected_uuid="uuid-original",
        )
        assert cc is None
        assert imposter.disconnected


# ---------------------------------------------------------------------------
# connect_speakers_direct — verification flows through to the fleet path
# ---------------------------------------------------------------------------
class TestConnectSpeakersDirect:
    def test_stale_ip_serving_other_device_drops_speaker(self, patch_listed):
        # "Office" saved at .5, but .5 now answers as "Kitchen".
        patch_listed(by_host={"10.0.0.5": FakeCast("Kitchen", "10.0.0.5")})
        cfg = {"Office": {"host": "10.0.0.5", "port": 8009}}
        result = discovery.connect_speakers_direct(cfg, ["Office"], timeout=1)
        assert "Office" not in result  # not silently played on the wrong speaker

    def test_correct_device_connects(self, patch_listed):
        patch_listed(by_host={"10.0.0.5": FakeCast("Office", "10.0.0.5")})
        cfg = {"Office": {"host": "10.0.0.5", "port": 8009}}
        result = discovery.connect_speakers_direct(cfg, ["Office"], timeout=1)
        assert "Office" in result


# ---------------------------------------------------------------------------
# find_speakers_by_name — full browse, matched by UUID or name
# ---------------------------------------------------------------------------
class TestFindSpeakersByName:
    def test_finds_only_requested_names(self, patch_browse):
        office = FakeCast("Office", "10.0.0.9")
        kitchen = FakeCast("Kitchen", "10.0.0.10")
        patch_browse([office, kitchen])
        found = discovery.find_speakers_by_name(["Office"])
        assert set(found) == {"Office"}
        assert found["Office"].cast_info.host == "10.0.0.9"
        # The browse returns every device; the ones we don't want must be
        # disconnected so their socket-client threads don't leak.
        assert kitchen.disconnected
        assert not office.disconnected

    def test_missing_name_absent_from_result(self, patch_browse):
        patch_browse([FakeCast("Office", "10.0.0.9")])
        found = discovery.find_speakers_by_name(["Office", "Garage"])
        assert set(found) == {"Office"}

    def test_empty_names_short_circuits(self, monkeypatch):
        called = {"v": False}

        def _boom(timeout=None):
            called["v"] = True
            return [], MagicMock()

        monkeypatch.setattr(discovery.pychromecast, "get_chromecasts", _boom, raising=False)
        assert discovery.find_speakers_by_name([]) == {}
        assert called["v"] is False  # never browses for an empty request

    def test_matches_by_uuid_across_rename(self, patch_browse):
        # Device now advertises a *new* friendly name but the same UUID.
        moved = FakeCast("Office (renamed)", "10.0.0.9", uuid="uuid-office")
        patch_browse([moved])
        found = discovery.find_speakers_by_name(
            ["Office"],
            identities={"Office": {"uuid": "uuid-office", "match_by": "device"}},
        )
        assert set(found) == {"Office"}
        assert found["Office"] is moved

    def test_name_mode_ignores_uuid(self, patch_browse):
        # match_by="name" follows the friendly name even if the UUID differs,
        # so a replacement unit adopting the same name takes over the slot.
        replacement = FakeCast("Office", "10.0.0.9", uuid="some-other-uuid")
        patch_browse([replacement])
        found = discovery.find_speakers_by_name(
            ["Office"],
            identities={"Office": {"uuid": "stale-uuid", "match_by": "name"}},
        )
        assert set(found) == {"Office"}
        assert found["Office"] is replacement


# ---------------------------------------------------------------------------
# scan_network_for_speakers — unicast, mDNS-independent
# ---------------------------------------------------------------------------
class TestScanNetwork:
    def test_recovers_device_by_name(self, monkeypatch, patch_listed):
        # Only .42 has the cast port open, and it answers as "Office".
        monkeypatch.setattr(discovery, "_tcp_port_open",
                            lambda h, p, t: h == "192.168.1.42")
        patch_listed(by_host={"192.168.1.42": FakeCast("Office", "192.168.1.42")})
        found = discovery.scan_network_for_speakers(
            ["Office"], ["192.168.1.41", "192.168.1.42", "192.168.1.43"],
        )
        assert set(found) == {"Office"}
        assert found["Office"].cast_info.host == "192.168.1.42"

    def test_ignores_open_host_with_wrong_name(self, monkeypatch, patch_listed):
        monkeypatch.setattr(discovery, "_tcp_port_open", lambda h, p, t: True)
        wrong = FakeCast("Kitchen", "192.168.1.50")
        patch_listed(by_host={"192.168.1.50": wrong})
        found = discovery.scan_network_for_speakers(["Office"], ["192.168.1.50"])
        assert found == {}
        assert wrong.disconnected

    def test_no_candidates_short_circuits(self, monkeypatch):
        called = False

        def _boom(*a, **k):
            nonlocal called
            called = True
            return False

        monkeypatch.setattr(discovery, "_tcp_port_open", _boom)
        assert discovery.scan_network_for_speakers(["Office"], []) == {}
        assert not called

    def test_tcp_port_open_against_real_socket(self):
        import socket as _socket
        srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            assert discovery._tcp_port_open("127.0.0.1", port, 1.0) is True
        finally:
            srv.close()
        # Port is closed now -> should be False
        assert discovery._tcp_port_open("127.0.0.1", port, 0.5) is False


# ---------------------------------------------------------------------------
# _candidate_hosts — subnet derivation
# ---------------------------------------------------------------------------
class TestCandidateHosts:
    def test_derives_slash24_from_saved_hosts(self):
        cfg = {"Office": {"host": "192.168.1.50"}}
        hosts = sched._candidate_hosts(cfg)
        assert "192.168.1.1" in hosts
        assert "192.168.1.254" in hosts
        assert len(hosts) == 254  # .1 .. .254, network/broadcast excluded

    def test_dedupes_same_subnet(self):
        cfg = {
            "Office": {"host": "192.168.1.50"},
            "Kitchen": {"host": "192.168.1.77"},
        }
        hosts = sched._candidate_hosts(cfg)
        assert len(hosts) == 254  # same /24, not doubled

    def test_skips_missing_and_invalid_hosts(self):
        cfg = {
            "A": {"host": None},
            "B": {},
            "C": {"host": "not-an-ip"},
        }
        assert sched._candidate_hosts(cfg) == []

    def test_respects_max_hosts_cap(self):
        cfg = {
            "A": {"host": "192.168.1.5"},
            "B": {"host": "10.0.0.5"},
        }
        hosts = sched._candidate_hosts(cfg, max_hosts=300)
        assert len(hosts) == 300


# ---------------------------------------------------------------------------
# carry-forward persistence — only host/port change
# ---------------------------------------------------------------------------
class TestCarryForward:
    def test_persist_updates_host_preserves_settings(self):
        speakers = {
            "Office": {
                "host": "192.168.1.50", "port": 8009,
                "enabled": True, "volume": 0.7,
                "schedule": {"Fajr": [0, 1, 2]},
            }
        }
        devices = {"Office": FakeCast("Office", "192.168.1.88")}  # moved IP
        changed = sched._persist_discovered_hosts(devices, speakers)
        assert changed is True
        office = speakers["Office"]
        assert office["host"] == "192.168.1.88"   # refreshed
        assert office["enabled"] is True          # carried forward
        assert office["volume"] == 0.7            # carried forward
        assert office["schedule"] == {"Fajr": [0, 1, 2]}  # carried forward

    def test_no_change_when_host_matches(self):
        speakers = {"Office": {"host": "192.168.1.50", "port": 8009, "volume": 0.7}}
        devices = {"Office": FakeCast("Office", "192.168.1.50")}
        assert sched._persist_discovered_hosts(devices, speakers) is False
        assert speakers["Office"]["volume"] == 0.7

    def test_backfills_missing_uuid_for_legacy_speaker(self):
        # A speaker added before UUID capture upgrades to stable identity the
        # first time we resolve it, even when its IP hasn't moved.
        speakers = {"Office": {"host": "192.168.1.50", "port": 8009, "enabled": True}}
        devices = {"Office": FakeCast("Office", "192.168.1.50", uuid="uuid-office")}
        assert sched._persist_discovered_hosts(devices, speakers) is True
        assert speakers["Office"]["uuid"] == "uuid-office"
        assert speakers["Office"]["enabled"] is True  # carried forward

    def test_does_not_overwrite_pinned_uuid(self):
        speakers = {"Office": {"host": "192.168.1.50", "port": 8009, "uuid": "pinned"}}
        devices = {"Office": FakeCast("Office", "192.168.1.50", uuid="different")}
        assert sched._persist_discovered_hosts(devices, speakers) is False
        assert speakers["Office"]["uuid"] == "pinned"


# ---------------------------------------------------------------------------
# _locate_speakers — full escalation + persistence
# ---------------------------------------------------------------------------
class TestLocateSpeakersEscalation:
    def _stub_config(self, monkeypatch, store):
        monkeypatch.setattr(sched, "load_config", lambda: {"speakers": store["speakers"]})

        def _save(cfg):
            store["speakers"] = cfg["speakers"]
            store["saved"] = True

        monkeypatch.setattr(sched, "save_config", _save)

    def test_direct_connect_happy_path_no_mdns_no_scan(self, monkeypatch):
        store = {"speakers": {"Office": {"host": "10.0.0.5", "port": 8009}}, "saved": False}
        self._stub_config(monkeypatch, store)
        monkeypatch.setattr(sched, "connect_speakers_direct",
                            lambda cfg, names, timeout=10: {"Office": FakeCast("Office", "10.0.0.5")})
        monkeypatch.setattr(sched, "find_speakers_by_name",
                            lambda *a, **k: pytest.fail("mDNS should not run"))
        monkeypatch.setattr(sched, "scan_network_for_speakers",
                            lambda *a, **k: pytest.fail("scan should not run"))
        out = sched._locate_speakers(store["speakers"], ["Office"])
        assert set(out) == {"Office"}

    def test_falls_through_to_mdns_and_persists_new_ip(self, monkeypatch):
        store = {"speakers": {"Office": {"host": "10.0.0.5", "port": 8009, "volume": 0.6}}, "saved": False}
        self._stub_config(monkeypatch, store)
        # Direct connect fails (stale IP), mDNS finds it at a new address.
        monkeypatch.setattr(sched, "connect_speakers_direct", lambda cfg, names, timeout=10: {})
        monkeypatch.setattr(sched, "find_speakers_by_name",
                            lambda names, timeout=15, identities=None: {"Office": FakeCast("Office", "10.0.0.99")})
        monkeypatch.setattr(sched, "scan_network_for_speakers",
                            lambda *a, **k: pytest.fail("scan should not run when browse succeeds"))
        out = sched._locate_speakers(store["speakers"], ["Office"])
        assert set(out) == {"Office"}
        assert store["saved"] is True
        assert store["speakers"]["Office"]["host"] == "10.0.0.99"  # healed
        assert store["speakers"]["Office"]["volume"] == 0.6        # carried forward

    def test_falls_through_to_scan_when_mdns_fails(self, monkeypatch):
        store = {"speakers": {"Office": {"host": "192.168.1.5", "port": 8009}}, "saved": False}
        self._stub_config(monkeypatch, store)
        monkeypatch.setattr(sched, "connect_speakers_direct", lambda cfg, names, timeout=10: {})
        monkeypatch.setattr(sched, "find_speakers_by_name", lambda names, timeout=15, identities=None: {})
        scan_calls = {}

        def _scan(names, hosts, **k):
            scan_calls["names"] = names
            scan_calls["host_count"] = len(hosts)
            return {"Office": FakeCast("Office", "192.168.1.123")}

        monkeypatch.setattr(sched, "scan_network_for_speakers", _scan)
        out = sched._locate_speakers(store["speakers"], ["Office"])
        assert set(out) == {"Office"}
        assert scan_calls["names"] == ["Office"]
        assert scan_calls["host_count"] == 254  # derived /24
        assert store["speakers"]["Office"]["host"] == "192.168.1.123"  # healed via scan
