# Copyright (c) 2026 Mechemsi. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

"""Equal peers sharing one queue by free capacity and temperature.

Every host publishes a snapshot (host metrics plus per-pool running/busy), fetches its
peers' snapshots, and runs the same pure `split` over them, spawning only its own share.
A peer that cannot be reached drops out; with none left the host scales as before.
See claudedocs/decisions/002-peer-federation-split-not-leader.md.
"""

import hmac
import json
import logging
import re
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from flask import Flask, jsonify, request

from rorch.hostmetrics import HostMetrics

log = logging.getLogger(__name__)

PEER_PATH = "/peer/state"
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*")

Snapshot = dict[str, Any]


@dataclass(frozen=True)
class PeerConfig:
    name: str
    url: str


@dataclass(frozen=True)
class FederationConfig:
    enabled: bool = False
    max_temp_c: float = 85.0
    rescue_after_seconds: int = 180
    stale_after_seconds: int = 60
    timeout_seconds: float = 2.0
    peers: tuple[PeerConfig, ...] = ()


def parse_config(raw: object) -> tuple[FederationConfig, list[str]]:
    """The `federation:` block. Any error disables federation: CI must keep running."""
    if raw is None:
        return FederationConfig(), []
    if not isinstance(raw, dict):
        return FederationConfig(), ["federation must be a mapping"]
    errors: list[str] = []
    try:
        cfg = FederationConfig(
            enabled=bool(raw.get("enabled", False)),
            max_temp_c=float(raw.get("max_temp_c", 85)),
            rescue_after_seconds=int(raw.get("rescue_after_seconds", 180)),
            stale_after_seconds=int(raw.get("stale_after_seconds", 60)),
            timeout_seconds=float(raw.get("timeout_seconds", 2)),
            peers=tuple(
                PeerConfig(str(p.get("name", "")).strip(), str(p.get("url", "")).rstrip("/"))
                for p in raw.get("peers") or []
            ),
        )
    except (TypeError, ValueError, AttributeError) as error:
        return FederationConfig(), [f"federation: {error}"]
    if not 30 <= cfg.max_temp_c <= 110:
        errors.append("federation.max_temp_c must be between 30 and 110")
    if cfg.rescue_after_seconds < 0 or cfg.stale_after_seconds <= 0:
        errors.append("federation rescue/stale seconds must be positive")
    if not 0 < cfg.timeout_seconds <= 10:
        errors.append("federation.timeout_seconds must be in (0, 10]")
    names = [p.name for p in cfg.peers]
    for peer in cfg.peers:
        if not _NAME_RE.fullmatch(peer.name):
            errors.append(
                f"federation peer name '{peer.name}' must be lowercase letters, digits, -"
            )
        if not re.fullmatch(r"https?://[^\s/]+(/\S*)?", peer.url):
            errors.append(f"federation peer '{peer.name}' url must be http(s)://host[:port]")
    if len(set(names)) != len(names):
        errors.append("federation peer names must be unique")
    if cfg.enabled and not cfg.peers:
        errors.append("federation.enabled needs at least one peer")
    return (FederationConfig(), errors) if errors else (cfg, [])


# ---------------------------------------------------------------- the split


@dataclass(frozen=True)
class Slot:
    """One host's standing for one pool."""

    name: str
    free_cores: int
    capacity: int  # how many queued jobs this host may still take


def split(queued: int, slots: list[Slot]) -> dict[str, int]:
    """Share `queued` jobs out by free cores, never past a host's capacity.

    Deterministic: every host computes the same answer from the same slots. The sum is
    min(queued, total capacity), so jobs are neither doubled nor left while room exists.
    """
    shares = {slot.name: 0 for slot in slots}
    open_slots = sorted(slots, key=lambda s: s.name)
    for _ in range(max(0, queued)):
        open_slots = [s for s in open_slots if shares[s.name] < s.capacity]
        if not open_slots:
            break
        # max() keeps the first of equals, and open_slots is name-ordered: ties go by name.
        best = max(open_slots, key=lambda s: (s.free_cores + 1) / (shares[s.name] + 1))
        shares[best.name] += 1
    return shares


def slot_for(snapshot: Snapshot, pool: str, max_temp_c: float) -> Slot | None:
    """A host's slot for `pool` from its snapshot; None when it does not run that pool."""
    counts = (snapshot.get("pools") or {}).get(pool)
    host = snapshot.get("host") or {}
    if not isinstance(counts, dict):
        return None
    temp = host.get("temp_c")
    hot = temp is not None and float(temp) >= max_temp_c
    busy, running = int(counts.get("busy", 0)), int(counts.get("running", 0))
    capacity = int(counts.get("max_runners", 0)) - busy
    global_free = snapshot.get("global_free")
    if global_free is not None:
        capacity = min(capacity, int(global_free) + running - busy)
    return Slot(
        name=str(snapshot.get("instance", "")),
        free_cores=int(host.get("free_cores", 0)),
        capacity=0 if hot else max(0, capacity),
    )


def build_snapshot(
    instance: str,
    version: str,
    metrics: HostMetrics,
    pools: dict[str, dict[str, int]],
    global_free: int | None,
) -> Snapshot:
    return {
        "instance": instance,
        "version": version,
        "ts": time.time(),
        "host": metrics.as_dict(),
        "pools": pools,
        "global_free": global_free,
    }


# ---------------------------------------------------------------- peers


def fetch_snapshot(url: str, token: str, timeout: float) -> Snapshot:
    req = urllib.request.Request(url + PEER_PATH, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = json.loads(response.read(256 * 1024))
    if not isinstance(data, dict) or not isinstance(data.get("pools"), dict):
        raise ValueError("not a peer snapshot")
    return data


@dataclass
class PeerView:
    snapshot: Snapshot | None = None
    ok_at: float = 0.0  # local monotonic time of the last good fetch
    error: str = ""


@dataclass
class Federation:
    config: FederationConfig
    token: str
    fetch: Callable[[str, str, float], Snapshot] = fetch_snapshot
    clock: Callable[[], float] = time.monotonic
    _own: Snapshot | None = None
    _peers: dict[str, PeerView] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def refresh(self, own: Snapshot) -> None:
        """Publish our snapshot and fetch every peer's. Called once per main-loop tick."""
        with self._lock:
            self._own = own
        for peer in self.config.peers:
            try:
                snapshot = self.fetch(peer.url, self.token, self.config.timeout_seconds)
            except Exception as error:  # a peer down must never stop our loop
                with self._lock:
                    view = self._peers.setdefault(peer.name, PeerView())
                    if not view.error:
                        log.warning("Peer %s unreachable: %s", peer.name, error)
                    view.error = str(error)[:200] or type(error).__name__
                continue
            with self._lock:
                self._peers[peer.name] = PeerView(snapshot, self.clock(), "")

    def own(self) -> Snapshot | None:
        with self._lock:
            return self._own

    def _fresh_peers(self) -> list[Snapshot]:
        """Peers seen within stale_after_seconds; one claiming our own name is ignored."""
        now = self.clock()
        with self._lock:
            me = (self._own or {}).get("instance")
            return [
                v.snapshot
                for v in self._peers.values()
                if v.snapshot is not None
                and now - v.ok_at <= self.config.stale_after_seconds
                and v.snapshot.get("instance") != me
            ]

    def is_hot(self) -> bool:
        own = self.own()
        temp = (own or {}).get("host", {}).get("temp_c")
        return self.config.enabled and temp is not None and float(temp) >= self.config.max_temp_c

    def covers(self, pool: str) -> bool:
        """Federated now: flag on, our snapshot has the pool, a fresh peer runs it."""
        own = self.own()
        if not self.config.enabled or own is None or pool not in own.get("pools", {}):
            return False
        return any(pool in p.get("pools", {}) for p in self._fresh_peers())

    def queue_input(self, pool: str, ages: list[float]) -> tuple[int, dict[str, int]]:
        """What this host feeds into its scaling formula as `queued`, and the full split."""
        own = self.own() or {}
        slots = [slot_for(s, pool, self.config.max_temp_c) for s in [own, *self._fresh_peers()]]
        shares = split(len(ages), [s for s in slots if s is not None])
        rescue = sum(1 for age in ages if age >= self.config.rescue_after_seconds)
        mine = shares.get(str(own.get("instance", "")), 0)
        return (0 if self.is_hot() else max(mine, rescue)), shares

    def status(self) -> dict[str, Any]:
        now = self.clock()
        with self._lock:
            views = {p.name: self._peers.get(p.name, PeerView()) for p in self.config.peers}
        peers = [
            {
                "name": peer.name,
                "url": peer.url,
                "ok": view.snapshot is not None and not view.error,
                "age_seconds": round(now - view.ok_at, 1) if view.snapshot else None,
                "error": view.error,
                "snapshot": view.snapshot,
            }
            for peer, view in ((p, views[p.name]) for p in self.config.peers)
        ]
        return {
            "enabled": self.config.enabled,
            "max_temp_c": self.config.max_temp_c,
            "hot": self.is_hot(),
            "self": self.own(),
            "peers": peers,
        }


# ---------------------------------------------------------------- listener


def create_peer_app(federation: Federation) -> Flask:
    """One read-only route. Separate port from the control API, which can start root containers.

    # ponytail: no brute-force lockout here; the token is 32+ random bytes and the
    # route only exposes load numbers. Reuse TokenGuard if it ever exposes more.
    """
    app = Flask("rorch-peer")

    @app.get(PEER_PATH)
    def peer_state() -> Any:
        header = request.headers.get("Authorization", "")
        supplied = header[len("Bearer ") :] if header.startswith("Bearer ") else ""
        if not federation.token or not hmac.compare_digest(supplied, federation.token):
            return jsonify(error="unauthorized"), 401
        own = federation.own()
        if own is None:
            return jsonify(error="starting"), 503
        return jsonify(own)

    return app
