# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

import random
import socket
import threading
from typing import Any

import pytest

from rorch.federation import (
    Federation,
    FederationConfig,
    PeerConfig,
    Slot,
    create_peer_app,
    fetch_snapshot,
    parse_config,
    slot_for,
    split,
    validate_snapshot,
)

HOST = {"cores": 8, "cpu_count": 8, "load1": 1.0, "mem_total_mb": 1, "mem_available_mb": 1}
PEER = PeerConfig("pve3", "http://192.168.1.245:8081")


def snap(instance: str, free: int = 4, temp: float | None = 50.0, **pool: int) -> dict[str, Any]:
    counts = {"running": 0, "busy": 0, "max_runners": 6} | pool
    return {
        "instance": instance,
        "host": {**HOST, "free_cores": free, "temp_c": temp},
        "pools": {"ci": counts},
        "global_free": None,
    }


# ------------------------------------------------------------------ split properties


def test_split_never_exceeds_queue_or_capacity_and_covers_when_room() -> None:
    rng = random.Random(1106)
    for _ in range(3000):
        slots = [
            Slot(name, rng.randint(0, 16), rng.randint(0, 8))
            for name in rng.sample(["a", "b", "c", "orch", "pve3"], rng.randint(1, 3))
        ]
        queued = rng.randint(0, 20)
        shares = split(queued, slots)
        total_capacity = sum(s.capacity for s in slots)
        assert sum(shares.values()) == min(queued, total_capacity)
        for slot in slots:
            assert 0 <= shares[slot.name] <= slot.capacity


def test_split_is_order_independent_and_tracks_free_cores() -> None:
    slots = [Slot("pve3", 2, 10), Slot("orch", 6, 10)]
    assert split(8, slots) == split(8, list(reversed(slots)))
    shares = split(8, slots)
    assert shares["orch"] > shares["pve3"] > 0
    assert split(1, [Slot("b", 3, 5), Slot("a", 3, 5)]) == {"a": 1, "b": 0}


def test_slot_hot_host_takes_nothing_unknown_temp_still_works() -> None:
    assert slot_for(snap("pve3", temp=85.0), "ci", 85).capacity == 0  # type: ignore[union-attr]
    assert slot_for(snap("pve3", temp=None), "ci", 85).capacity == 6  # type: ignore[union-attr]
    assert slot_for(snap("pve3"), "other", 85) is None


def test_slot_capacity_respects_busy_and_global_cap() -> None:
    s = snap("orch", running=4, busy=3) | {"global_free": 1}
    # max 6 - busy 3 = 3, but only 1 free global slot + 1 idle runner = 2
    assert slot_for(s, "ci", 85).capacity == 2  # type: ignore[union-attr]


# ------------------------------------------------------------------ config


def test_parse_config_defaults_and_valid_block() -> None:
    assert parse_config(None) == (FederationConfig(), [])
    cfg, errors = parse_config(
        {"enabled": True, "max_temp_c": 80, "peers": [{"name": "pve3", "url": PEER.url + "/"}]}
    )
    assert errors == []
    assert cfg.enabled and cfg.max_temp_c == 80 and cfg.peers == (PEER,)


@pytest.mark.parametrize(
    "raw",
    [
        "yes",
        {"enabled": True},
        {"max_temp_c": 200},
        {"timeout_seconds": 0},
        {"peers": [{"name": "Bad Name", "url": PEER.url}]},
        {"peers": [{"name": "pve3", "url": "ftp://x"}]},
        {"peers": [{"name": "a", "url": PEER.url}, {"name": "a", "url": PEER.url}]},
        {"max_temp_c": "hot"},
    ],
)
def test_parse_config_errors_disable_federation(raw: object) -> None:
    cfg, errors = parse_config(raw)
    assert errors
    assert cfg == FederationConfig()


# ------------------------------------------------------------------ peers


def federation(fetch: Any, clock: Any = lambda: 100.0, **cfg: Any) -> Federation:
    config = FederationConfig(enabled=True, peers=(PEER,), **cfg)
    return Federation(config, "tok", fetch=fetch, clock=clock)


def test_unreachable_peer_falls_back_to_solo() -> None:
    def down(*_: Any) -> dict[str, Any]:
        raise TimeoutError("timed out")

    fed = federation(down)
    fed.refresh(snap("orch"))
    assert not fed.covers("ci")
    status = fed.status()["peers"][0]
    assert status["ok"] is False and "timed out" in status["error"]


def test_stale_peer_drops_out() -> None:
    now = [100.0]
    fed = federation(lambda *_: snap("pve3"), clock=lambda: now[0], stale_after_seconds=60)
    fed.refresh(snap("orch"))
    assert fed.covers("ci")
    now[0] += 61
    assert not fed.covers("ci")


def test_flag_off_or_same_instance_is_solo() -> None:
    off = Federation(FederationConfig(peers=(PEER,)), "tok", fetch=lambda *_: snap("pve3"))
    off.refresh(snap("orch"))
    assert not off.covers("ci")
    twin = federation(lambda *_: snap("orch"))
    twin.refresh(snap("orch"))
    assert not twin.covers("ci")


def test_two_hosts_together_take_exactly_the_queue() -> None:
    orch, pve3 = snap("orch", free=6), snap("pve3", free=3)
    a, b = federation(lambda *_: pve3), federation(lambda *_: orch)
    a.refresh(orch)
    b.refresh(pve3)
    ages = [5.0] * 7
    (mine_a, split_a), (mine_b, split_b) = a.queue_input("ci", ages), b.queue_input("ci", ages)
    assert split_a == split_b
    assert mine_a + mine_b == 7 and mine_a > mine_b


def test_old_jobs_are_rescued_and_hot_host_spawns_nothing() -> None:
    fed = federation(lambda *_: snap("pve3", free=16))
    fed.refresh(snap("orch", free=0))
    queued, shares = fed.queue_input("ci", [500.0, 500.0, 1.0])
    assert shares["orch"] < 2 and queued == 2  # 2 jobs older than rescue_after_seconds
    hot = federation(lambda *_: snap("pve3"))
    hot.refresh(snap("orch", temp=90.0))
    assert hot.is_hot()
    assert hot.queue_input("ci", [500.0])[0] == 0


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda s: s["pools"]["ci"].update(busy="3"),
        lambda s: s["pools"]["ci"].update(running=-1),
        lambda s: s["pools"]["ci"].update(max_runners=10**9),
        lambda s: s["pools"]["ci"].pop("busy"),
        lambda s: s["host"].update(free_cores=float("nan")),
        lambda s: s["host"].update(load1=float("inf")),
        lambda s: s["host"].update(temp_c=900),
        lambda s: s["host"].update(cores=True),
        lambda s: s["host"].pop("cores"),
        lambda s: s.update(global_free=-5),
        lambda s: s.update(instance="../etc"),
        lambda s: s.update(pools=["ci"]),
        lambda s: s.pop("host"),
    ],
)
def test_malformed_peer_snapshot_means_unreachable(corrupt: Any) -> None:
    bad = snap("pve3")
    corrupt(bad)
    fed = federation(lambda *_: bad)
    fed.refresh(snap("orch"))
    assert not fed.covers("ci")
    assert fed.status()["peers"][0]["ok"] is False


def test_valid_snapshot_round_trips() -> None:
    good = snap("pve3", temp=None) | {"ts": 1.0, "version": "1.5.0"}
    assert validate_snapshot(good)["host"]["temp_c"] is None
    assert validate_snapshot(good)["pools"] == good["pools"]


def test_fetch_times_out_on_a_silent_peer() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        with pytest.raises(OSError):  # socket.timeout / TimeoutError are OSError
            fetch_snapshot(f"http://127.0.0.1:{port}", "tok", 0.2)
    finally:
        listener.close()


# ------------------------------------------------------------------ listener


def test_peer_app_requires_the_token() -> None:
    fed = federation(lambda *_: snap("pve3"))
    client = create_peer_app(fed).test_client()
    assert client.get("/peer/state").status_code == 401
    auth = {"Authorization": "Bearer tok"}
    assert client.get("/peer/state", headers=auth).status_code == 503
    fed.refresh(snap("orch"))
    assert client.get("/peer/state", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/peer/state", headers=auth).get_json()["instance"] == "orch"


def test_refresh_survives_concurrent_reads() -> None:
    fed = federation(lambda *_: snap("pve3"))
    threads = [threading.Thread(target=fed.refresh, args=(snap("orch"),)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert fed.covers("ci")
