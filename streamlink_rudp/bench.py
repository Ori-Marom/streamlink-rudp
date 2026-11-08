"""Reproducible experiments and charts.

* ``loss_sweep``  - goodput and retransmission overhead vs packet loss,
                    SACK + adaptive RTO against the Go-Back-N + fixed-RTO baseline.
* ``cwnd_trace``  - congestion window over time through a bandwidth bottleneck.
* ``abr_session`` - a real-time streaming session while the available bandwidth changes.

Charts need matplotlib (``pip install matplotlib``); the experiments themselves do not.
"""
from __future__ import annotations

import json
import os
import socket
import statistics
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from .netem import NetemConfig, NetworkEmulator
from .transport import ReceiveStats, Sender, SenderConfig, SenderStats, receive

# Reference palette (light mode), see docs: categorical slots in fixed order.
INK, INK_2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"

VARIANTS = {
    "SACK + adaptive RTO": SenderConfig(sack=True, adaptive_rto=True),
    "Go-Back-N + fixed RTO": SenderConfig(sack=False, adaptive_rto=False),
}


@dataclass
class TransferResult:
    sender: SenderStats
    receiver: ReceiveStats
    link: object


def loopback_transfer(data: bytes, sender_config: SenderConfig | None = None,
                      forward: NetemConfig | None = None, reverse: NetemConfig | None = None,
                      conn_id: int = 0x5EED) -> TransferResult:
    """Transfer ``data`` over loopback through emulated links (forward = data direction)."""
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    control = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for s in (server, client, control):
        s.bind(("127.0.0.1", 0))
    fwd, rev = NetworkEmulator(forward), NetworkEmulator(reverse)
    result: dict = {}

    def serve():
        control.settimeout(10)
        _, addr = control.recvfrom(2048)
        try:
            result["sender"] = Sender(fwd.wrap(server), addr, conn_id, data, sender_config).run()
        except Exception as exc:  # surfaced to the caller below
            result["error"] = exc

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        received, rstats = receive(rev.wrap(client), control.getsockname(), b"GET", conn_id)
        thread.join(60)
    finally:
        fwd.close(); rev.close()
        for s in (server, client, control):
            s.close()
    if "error" in result:
        raise result["error"]
    if "sender" not in result:
        raise TimeoutError("sender did not finish")
    if received != data:
        raise AssertionError("received data differs from sent data")
    return TransferResult(result["sender"], rstats, fwd.stats)


# -- experiments ------------------------------------------------------------------------------
def loss_sweep(losses=(0.0, 0.01, 0.02, 0.05, 0.10), size: int = 1_000_000, seeds=(1, 2, 3),
               delay_ms: float = 20.0, progress=print) -> list[dict]:
    data = os.urandom(size)
    rows = []
    for name, cfg in VARIANTS.items():
        for loss in losses:
            goodputs, overheads, times = [], [], []
            for seed in seeds:
                fwd = NetemConfig(loss=loss, delay_ms=delay_ms, jitter_ms=2, seed=seed)
                rev = NetemConfig(loss=loss, delay_ms=delay_ms, jitter_ms=2, seed=seed + 100)
                r = loopback_transfer(data, cfg, fwd, rev)
                goodputs.append(r.receiver.throughput_bps / 1e6)
                overheads.append(r.sender.retransmission_ratio * 100)
                times.append(r.receiver.duration)
            row = {"variant": name, "loss": loss, "goodput_mbps": statistics.mean(goodputs),
                   "retransmit_pct": statistics.mean(overheads), "seconds": statistics.mean(times)}
            rows.append(row)
            progress(f"  {name:<24} loss={loss:5.1%}  goodput={row['goodput_mbps']:6.2f} Mbps  "
                     f"retransmissions={row['retransmit_pct']:5.1f}%")
    return rows


def cwnd_trace(size: int = 6_000_000, rate_kbps: float = 8000, delay_ms: float = 20.0,
               queue_ms: float = 40.0) -> TransferResult:
    fwd = NetemConfig(rate_kbps=rate_kbps, delay_ms=delay_ms, queue_ms=queue_ms, seed=7)
    rev = NetemConfig(delay_ms=delay_ms, seed=8)
    return loopback_transfer(os.urandom(size), SenderConfig(trace=True), fwd, rev)


def abr_session(media_dir: Path, profile: list[tuple[float, float]], out_dir: Path,
                delay_ms: float = 20.0, progress=print):
    from .client import StreamClient
    from .server import MediaServer

    netem = NetemConfig(delay_ms=delay_ms, bandwidth_profile=profile, queue_ms=80, seed=11)
    with MediaServer(media_dir, udp_port=0, tcp_port=None, netem=netem) as server:
        server.emulator.reset_clock()
        client = StreamClient(udp_port=server.udp_port)

        def on_segment(rec, model):
            progress(f"  t={rec.at:5.1f}s  segment {rec.index:2d}  {rec.rendition:>5}  "
                     f"measured {rec.throughput_kbps:7.0f} kbps  buffer {rec.buffer_before:4.1f}s")

        report = client.stream(out_dir, realtime=True, make_mp4=False, on_segment=on_segment)
    return report


# -- charts -----------------------------------------------------------------------------------------
def _pyplot():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "axes.edgecolor": GRID, "axes.labelcolor": INK_2, "axes.titlecolor": INK,
        "axes.titlesize": 12, "axes.titleweight": "bold", "axes.titlelocation": "left",
        "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
        "grid.color": GRID, "grid.linewidth": 0.8, "xtick.color": INK_2, "ytick.color": INK_2,
        "font.size": 10, "legend.frameon": False, "legend.labelcolor": INK_2,
        "lines.linewidth": 2, "lines.solid_capstyle": "round",
    })
    return plt


def plot_loss_sweep(rows: list[dict], path: Path) -> None:
    plt = _pyplot()
    fig, (a, b) = plt.subplots(1, 2, figsize=(10, 3.8))
    for (name, color), marker in zip(zip(VARIANTS, (BLUE, ORANGE)), ("o", "s")):
        sel = [r for r in rows if r["variant"] == name]
        x = [r["loss"] * 100 for r in sel]
        a.plot(x, [r["goodput_mbps"] for r in sel], marker=marker, markersize=6, color=color, label=name)
        b.plot(x, [r["retransmit_pct"] for r in sel], marker=marker, markersize=6, color=color, label=name)
    a.set_title("Goodput vs packet loss")
    a.set_xlabel("Packet loss (%) each direction, RTT 40 ms")
    a.set_ylabel("Goodput (Mbps)")
    a.set_yscale("log")
    b.set_title("Retransmission overhead")
    b.set_xlabel("Packet loss (%) each direction, RTT 40 ms")
    b.set_ylabel("Retransmitted segments (%)")
    a.legend(loc="upper right")
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def plot_cwnd(result: TransferResult, rate_kbps: float, path: Path) -> None:
    plt = _pyplot()
    trace = result.sender.trace
    t = [p[0] for p in trace]
    fig, ax = plt.subplots(figsize=(10, 3.6))
    ax.plot(t, [p[1] for p in trace], color=BLUE, label="Congestion window (cwnd)")
    ax.plot(t, [min(p[2], 200) for p in trace], color=INK_2, linewidth=1.2, linestyle="--",
            label="Slow-start threshold")
    events = [e for e in result.sender.events if e[1] == "fast-retransmit"]
    if events:
        ax.scatter([e[0] for e in events], [0.6] * len(events), marker="v", s=36, color=ORANGE,
                   zorder=3, label="Loss detected (fast retransmit)", transform=ax.get_xaxis_transform())
    ymax = max(p[1] for p in trace) * 1.25
    ax.set_ylim(0, ymax)
    ax.set_title(f"Congestion control through a {rate_kbps / 1000:g} Mbps bottleneck")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Window (segments)")
    ax.legend(loc="upper left", ncol=3)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def plot_abr(report, profile: list[tuple[float, float]], path: Path) -> None:
    plt = _pyplot()
    fig, (a, b) = plt.subplots(2, 1, figsize=(10, 5.6), sharex=True,
                               gridspec_kw={"height_ratios": [2, 1]})
    end = report.wall_seconds
    xs, ys = [], []
    for i, (at, kbps) in enumerate(profile):
        nxt = profile[i + 1][0] if i + 1 < len(profile) else end
        xs += [at, min(nxt, end)]
        ys += [kbps, kbps]
    a.plot(xs, ys, color=INK_2, linewidth=1.5, linestyle="--", label="Available bandwidth")
    a.step([s.at for s in report.segments], [s.bitrate_kbps for s in report.segments],
           where="post", color=BLUE, label="Chosen rendition bitrate")
    a.scatter([s.at for s in report.segments], [s.throughput_kbps for s in report.segments],
              s=18, color=ORANGE, zorder=3, label="Measured segment throughput")
    a.set_title("Adaptive bitrate over RUDP while bandwidth changes")
    a.set_ylabel("kbps")
    a.legend(loc="upper right", ncol=3)
    a.set_ylim(0, max(ys) * 1.35)
    b.plot([s.at for s in report.segments], [s.buffer_before for s in report.segments],
           color=AQUA, marker="o", markersize=4)
    b.set_title("Playback buffer")
    b.set_ylabel("Seconds")
    b.set_xlabel("Session time (s)")
    b.set_ylim(bottom=0)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def run_all(out_dir: Path, media_dir: Path | None = None, quick: bool = False, progress=print) -> dict:
    """Run every experiment, write charts + results.json into ``out_dir``."""
    from . import media

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results: dict = {}

    progress("[1/3] Loss sweep: SACK + adaptive RTO vs Go-Back-N + fixed RTO")
    rows = loss_sweep(losses=(0.0, 0.02, 0.05) if quick else (0.0, 0.01, 0.02, 0.05, 0.10),
                      size=300_000 if quick else 1_000_000, seeds=(1,) if quick else (1, 2, 3))
    results["loss_sweep"] = rows

    progress("[2/3] Congestion window trace through an 8 Mbps bottleneck")
    trace = cwnd_trace(size=2_000_000 if quick else 6_000_000)
    results["cwnd"] = {"goodput_mbps": trace.receiver.throughput_bps / 1e6,
                       "fast_recoveries": trace.sender.fast_recoveries,
                       "timeouts": trace.sender.timeouts, "queue_drops": trace.link.dropped_queue}
    progress(f"  goodput {results['cwnd']['goodput_mbps']:.2f} Mbps, "
             f"{trace.sender.fast_recoveries} fast recoveries, {trace.sender.timeouts} timeouts")

    progress("[3/3] Adaptive streaming session with a changing bandwidth profile")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        if media_dir is None:
            ladder = media.TINY_LADDER if quick else media.DEFAULT_LADDER
            src = media.synthesize_source(tmp / "source.mp4", duration=12 if quick else 40)
            media_dir = tmp / "media"
            manifest = media.prepare(src, media_dir, ladder=ladder)
        else:
            manifest = json.loads((Path(media_dir) / media.MANIFEST_NAME).read_text())
        rates = [r["bandwidth"] / 1000 for r in manifest["renditions"]]
        dur = manifest["duration"]
        profile = [(0.0, rates[-1] * 1.6), (dur * 0.3, rates[0] * 1.5), (dur * 0.6, rates[-1] * 1.6)]
        report = abr_session(Path(media_dir), profile, tmp / "session", progress=progress)
    results["abr"] = json.loads(report.to_json())
    results["abr"]["profile"] = profile

    (out_dir / "results.json").write_text(json.dumps(results, indent=2))
    try:
        plot_loss_sweep(rows, out_dir / "loss_sweep.png")
        plot_cwnd(trace, 8000, out_dir / "cwnd_trace.png")
        plot_abr(report, profile, out_dir / "abr_session.png")
        progress(f"charts written to {out_dir}")
    except ImportError:
        progress("matplotlib is not installed: wrote results.json only (pip install matplotlib)")
    return results
