"""Command-line interface: ``python -m streamlink_rudp <command>``."""
from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from pathlib import Path

from . import __version__, discovery, media
from .netem import NetemConfig
from .transport import SenderConfig


def _add_netem(p: argparse.ArgumentParser, what: str) -> None:
    g = p.add_argument_group(f"network emulation ({what})")
    g.add_argument("--loss", type=float, default=0.0, help="packet loss probability, e.g. 0.05")
    g.add_argument("--delay", type=float, default=0.0, metavar="MS", help="one-way delay in ms")
    g.add_argument("--jitter", type=float, default=0.0, metavar="MS", help="delay jitter in ms")
    g.add_argument("--reorder", type=float, default=0.0, help="reordering probability")
    g.add_argument("--duplicate", type=float, default=0.0, help="duplication probability")
    g.add_argument("--corrupt", type=float, default=0.0, help="corruption probability")
    g.add_argument("--rate", type=float, default=None, metavar="KBPS", help="bottleneck bandwidth")
    g.add_argument("--bw-profile", default="", metavar="T:KBPS,...",
                   help="bandwidth over time, e.g. 0:6000,15:1200,30:6000")
    g.add_argument("--seed", type=int, default=None, help="random seed for reproducible runs")


def _netem(args) -> NetemConfig:
    return NetemConfig(loss=args.loss, delay_ms=args.delay, jitter_ms=args.jitter, reorder=args.reorder,
                       duplicate=args.duplicate, corrupt=args.corrupt, rate_kbps=args.rate,
                       bandwidth_profile=NetemConfig.parse_profile(args.bw_profile), seed=args.seed)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="streamlink-rudp",
                                     description="Adaptive video streaming over a custom reliable UDP transport.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("prepare", help="encode a video into a multi-bitrate segmented stream")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--input", type=Path, help="source video file")
    src.add_argument("--synthetic", action="store_true", help="generate a test video with ffmpeg")
    p.add_argument("--duration", type=float, default=30.0, help="synthetic video length (s)")
    p.add_argument("--out", type=Path, default=Path("media"), help="output directory")
    p.add_argument("--segment", type=float, default=2.0, help="segment duration (s)")
    p.add_argument("--ladder", choices=["default", "tiny"], default="default",
                   help="default = 360p/720p/1080p, tiny = fast 180p/360p")
    p.add_argument("--no-label", action="store_true", help="do not burn the rendition name into frames")

    p = sub.add_parser("serve", help="serve a prepared media directory over RUDP and TCP")
    p.add_argument("--media", type=Path, default=Path("media"))
    p.add_argument("--host", default="127.0.0.1", help="address to bind (0.0.0.0 for LAN)")
    p.add_argument("--udp-port", type=int, default=8888)
    p.add_argument("--tcp-port", type=int, default=9999)
    p.add_argument("--no-sack", action="store_true", help="Go-Back-N baseline instead of SACK")
    p.add_argument("--fixed-rto", action="store_true", help="fixed 1 s timeout instead of RFC 6298")
    _add_netem(p, "applied to data sent by the server")

    p = sub.add_parser("stream", help="download (and optionally play) a stream adaptively")
    p.add_argument("--server", default="127.0.0.1", help="server address")
    p.add_argument("--udp-port", type=int, default=8888)
    p.add_argument("--tcp-port", type=int, default=9999)
    p.add_argument("--transport", choices=["rudp", "tcp"], default="rudp")
    p.add_argument("--out", type=Path, default=Path("downloads"))
    p.add_argument("--play", action="store_true", help="play live while downloading")
    p.add_argument("--player", choices=["ffplay", "mpv", "vlc"], help="player to use with --play")
    p.add_argument("--realtime", action="store_true", help="pace downloads like a player (implied by --play)")
    p.add_argument("--rendition", help="pin one rendition (e.g. 720p) instead of adapting")
    p.add_argument("--no-mp4", action="store_true", help="keep only stream.ts")
    p.add_argument("--discover", action="store_true",
                   help="find the server through the DHCP + DNS services first")
    _add_netem(p, "applied to ACKs sent by the client")

    p = sub.add_parser("demo", help="all-in-one: discovery + server + adaptive streaming")
    p.add_argument("--out", type=Path, default=Path("downloads"))
    p.add_argument("--play", action="store_true", help="watch the stream live")
    p.add_argument("--quick", action="store_true", help="small, fast-encoding video")
    p.add_argument("--duration", type=float, default=30.0)
    p.add_argument("--bw-profile", default=None, metavar="T:KBPS,...",
                   help="server bandwidth over time (default: high, then a drop, then recovery)")
    p.add_argument("--loss", type=float, default=0.0, help="extra random loss on top of bottleneck drops")
    p.add_argument("--delay", type=float, default=20.0, metavar="MS")

    p = sub.add_parser("bench", help="run experiments and draw charts")
    p.add_argument("--out", type=Path, default=Path("docs/images"))
    p.add_argument("--media", type=Path, default=None, help="use an already prepared media directory")
    p.add_argument("--quick", action="store_true", help="smaller, faster runs")

    p = sub.add_parser("dhcp", help="run the DHCP-style address allocator")
    p.add_argument("--port", type=int, default=discovery.DEFAULT_DHCP_PORT)
    p = sub.add_parser("dns", help="run the DNS resolver (UDP + DNS-over-HTTP)")
    p.add_argument("--port", type=int, default=discovery.DEFAULT_DNS_PORT)
    p.add_argument("--doh-port", type=int, default=discovery.DEFAULT_DOH_PORT)
    p.add_argument("--record", action="append", default=[], metavar="NAME=IP",
                   help=f"extra record (default {discovery.APP_DOMAIN}=127.0.0.1)")
    return parser


def _print_segment(rec, model) -> None:
    bar = "#" * max(1, round(rec.bitrate_kbps / 250))
    print(f"  seg {rec.index:3d}  {rec.rendition:>6}  {bar:<20} {rec.throughput_kbps:8.0f} kbps measured"
          f"   buffer {model.level():5.1f}s")


def _print_report(report) -> None:
    print()
    print(f"  Downloaded   {len(report.segments)} segments, {report.bytes / 1e6:.1f} MB "
          f"in {report.wall_seconds:.1f}s over {report.transport.upper()}")
    print(f"  Bitrate      avg {report.average_bitrate_kbps:.0f} kbps, {report.switches} quality switches")
    print(f"  Playback     startup {report.startup_delay or 0:.2f}s, {report.stalls} stalls "
          f"({report.stall_seconds:.1f}s)")
    print(f"  Saved        {report.output_mp4 or report.output_ts}")


def cmd_prepare(args) -> int:
    ladder = media.TINY_LADDER if args.ladder == "tiny" else media.DEFAULT_LADDER
    source = args.input
    with tempfile.TemporaryDirectory() as tmp:
        if args.synthetic:
            print(f"Generating a {args.duration:g}s test video...")
            top = ladder[-1]
            source = media.synthesize_source(Path(tmp) / "synthetic.mp4", args.duration,
                                             top.width, top.height)
        print(f"Encoding {len(ladder)} renditions into {args.out}/ ...")
        manifest = media.prepare(source, args.out, ladder, args.segment, label=not args.no_label)
    for r in manifest["renditions"]:
        print(f"  {r['id']:>6}  {r['width']}x{r['height']}  {r['bandwidth'] / 1000:6.0f} kbps  "
              f"{len(r['segments'])} segments")
    print(f"Done: {manifest['duration']:.1f}s of video. Serve it with:  python -m streamlink_rudp serve --media {args.out}")
    return 0


def cmd_serve(args) -> int:
    from .server import MediaServer
    cfg = SenderConfig(sack=not args.no_sack, adaptive_rto=not args.fixed_rto)
    server = MediaServer(args.media, args.host, args.udp_port, args.tcp_port, _netem(args), cfg)
    print(f"Serving {args.media} on {args.host}  RUDP udp/{server.udp_port}  TCP tcp/{server.tcp_port}"
          "  (Ctrl+C to stop)")
    server.serve_forever()
    return 0


def cmd_stream(args) -> int:
    from .client import StreamClient
    host = args.server
    if args.discover:
        ip = discovery.dhcp_lease((args.server, discovery.DEFAULT_DHCP_PORT))
        print(f"DHCP lease: {ip}")
        host = discovery.dns_lookup(discovery.APP_DOMAIN, (args.server, discovery.DEFAULT_DNS_PORT))
        print(f"DNS: {discovery.APP_DOMAIN} -> {host}")
    client = StreamClient(host, args.udp_port, args.tcp_port, args.transport, _netem(args))
    report = client.stream(args.out, play=args.play, player=args.player,
                           realtime=args.realtime or args.play, rendition=args.rendition,
                           make_mp4=not args.no_mp4, on_segment=_print_segment)
    _print_report(report)
    return 0


def cmd_demo(args) -> int:
    from .client import Player, StreamClient
    from .server import MediaServer

    if args.play and Player.find() is None:
        print("No player found for --play: install ffplay (comes with ffmpeg), mpv or VLC.")
        return 1
    ladder = media.TINY_LADDER if args.quick else media.DEFAULT_LADDER
    duration = 12.0 if args.quick and args.duration == 30.0 else args.duration
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        print(f"[1/4] Preparing a {duration:g}s test video in {len(ladder)} qualities (ffmpeg)...")
        top = ladder[-1]
        src = media.synthesize_source(tmp / "source.mp4", duration, top.width, top.height)
        manifest = media.prepare(src, tmp / "media", ladder)
        rates = [r["bandwidth"] / 1000 for r in manifest["renditions"]]
        if args.bw_profile:
            profile = NetemConfig.parse_profile(args.bw_profile)
        else:
            d = manifest["duration"]
            profile = [(0.0, rates[-1] * 1.6), (d * 0.25, rates[0] * 1.5), (d * 0.5, rates[-1] * 1.6)]
        print("      bandwidth profile: " + ", ".join(f"{t:.0f}s: {k / 1000:.1f} Mbps" for t, k in profile))

        print("[2/4] Starting DHCP, DNS and the media server...")
        dhcp = discovery.DHCPServer(port=0).start()
        dns = discovery.DNSServer(port=0, enable_doh=False).start()
        netem = NetemConfig(loss=args.loss, delay_ms=args.delay, jitter_ms=2,
                            bandwidth_profile=profile, queue_ms=80)
        server = MediaServer(tmp / "media", udp_port=0, tcp_port=None, netem=netem).start()
        try:
            print("[3/4] Discovery")
            ip = discovery.dhcp_lease(("127.0.0.1", dhcp.port))
            print(f"      DHCP: leased {ip}")
            host = discovery.dns_lookup(discovery.APP_DOMAIN, ("127.0.0.1", dns.port))
            print(f"      DNS:  {discovery.APP_DOMAIN} -> {host}")
            print("[4/4] Streaming over RUDP with adaptive bitrate"
                  + (" (playing live)" if args.play else ""))
            server.emulator.reset_clock()
            client = StreamClient(host, server.udp_port)
            report = client.stream(args.out, play=args.play, realtime=True, max_buffer=10.0,
                                   on_segment=_print_segment)
            _print_report(report)
        finally:
            server.stop(); dhcp.stop(); dns.stop()
    return 0


def cmd_bench(args) -> int:
    from .bench import run_all
    run_all(args.out, args.media, args.quick)
    return 0


def cmd_dhcp(args) -> int:
    print(f"DHCP allocator on udp/{args.port}")
    discovery.DHCPServer(port=args.port).serve_forever()
    return 0


def cmd_dns(args) -> int:
    records = {discovery.APP_DOMAIN: "127.0.0.1"}
    records.update(dict(r.split("=", 1) for r in args.record))
    print(f"DNS resolver on udp/{args.port}, DNS-over-HTTP on tcp/{args.doh_port}")
    discovery.DNSServer(port=args.port, doh_port=args.doh_port, records=records).serve_forever()
    return 0


COMMANDS = {"prepare": cmd_prepare, "serve": cmd_serve, "stream": cmd_stream, "demo": cmd_demo,
            "bench": cmd_bench, "dhcp": cmd_dhcp, "dns": cmd_dns}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        return COMMANDS[args.command](args)
    except KeyboardInterrupt:
        return 130
    except (media.MediaError, OSError, RuntimeError, LookupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
