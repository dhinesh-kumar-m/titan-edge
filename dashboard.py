#!/usr/bin/env python3
"""Watches the Titan server and writes what the edge nginx serves.

Standalone: never calls into the streams app, never called by it. The only
thing the two agree on is the layout of a stream's directory - an "index"
file holding its port number, and its HLS files under livestream/.
"""
from __future__ import annotations

import getpass
import grp
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
STREAMS_ROOT = os.environ.get("STREAMS_ROOT", "/srv/live-flow/streams")
STATUS_DIR = os.environ.get("STATUS_DIR", "/srv/live-flow/status")
SERVER_IP = os.environ.get("SERVER_IP", "122.186.108.52")

MAX_HISTORY_ROWS = 17280  # 24 hours at one sample every 5 seconds

HISTORY_HEADER = (
    "epoch,streams,stalled,load,nproc,busiest_core_pct,mem_total_kb,mem_avail_kb,"
    "swap_total_kb,swap_free_kb,disk_avail_kb,disk_sectors_read,disk_sectors_written,"
    "disk_io_ms,disk_ios,rx_bytes,tx_bytes,rx_dropped,tx_dropped,rx_errors,tx_errors,cpu_all_pct"
)


@dataclass
class StatusPageResult:
    stream_count: int
    output_path: str


def _default_interface() -> str:
    route_output = subprocess.run(["ip", "-4", "route", "show", "default"], capture_output=True, text=True).stdout
    match = re.search(r"dev (\S+)", route_output)
    return match.group(1) if match else ""


def _read_net_stat(iface: str, name: str) -> int:
    try:
        return int(Path(f"/sys/class/net/{iface}/statistics/{name}").read_text().strip())
    except (OSError, ValueError):
        return 0


def _cpu_lines_from_proc_stat() -> dict:
    lines = {}
    for line in Path("/proc/stat").read_text().splitlines():
        parts = line.split()
        if parts and (parts[0] == "cpu" or (parts[0].startswith("cpu") and parts[0][3:].isdigit())):
            lines[parts[0]] = [int(value) for value in parts[1:]]
    return lines


def _cpu_busy_pct(previous: list, current: list) -> float:
    total_delta = sum(current) - sum(previous)
    if total_delta <= 0:
        return 0.0
    idle_delta = current[3] - previous[3] if len(current) > 3 and len(previous) > 3 else 0
    return (total_delta - idle_delta) / total_delta * 100


def sample() -> None:
    streams_root = Path(STREAMS_ROOT)
    status_dir = Path(STATUS_DIR)
    status_dir.mkdir(parents=True, exist_ok=True)
    history_file = status_dir / "history.csv"
    cpu_state_file = status_dir / ".sample_state"

    if history_file.exists() and history_file.stat().st_size:
        first_line = history_file.read_text().splitlines()[0]
        if first_line != HISTORY_HEADER:
            history_file.rename(status_dir / f"history.csv.old-{int(time.time())}")
    if not history_file.exists():
        history_file.write_text(HISTORY_HEADER + "\n")

    iface = _default_interface()
    now = int(time.time())

    stream_count = stalled_count = 0
    if streams_root.is_dir():
        for stream_dir in streams_root.iterdir():
            if not stream_dir.is_dir() or not (stream_dir / "index").exists():
                continue
            stream_count += 1
            playlist = stream_dir / "livestream" / "video-720p.m3u8"
            try:
                if now - playlist.stat().st_mtime > 15:
                    stalled_count += 1
            except OSError:
                stalled_count += 1

    try:
        load_1min = os.getloadavg()[0]
    except OSError:
        load_1min = 0.0
    nproc = os.cpu_count() or 1

    busiest_core_pct = 0.0
    cpu_all_pct = 0.0
    current_cpu_lines = _cpu_lines_from_proc_stat()
    if cpu_state_file.exists():
        previous_cpu_lines = {}
        for line in cpu_state_file.read_text().splitlines():
            parts = line.split()
            if parts:
                previous_cpu_lines[parts[0]] = [int(value) for value in parts[1:]]
        for key, current_values in current_cpu_lines.items():
            previous_values = previous_cpu_lines.get(key)
            if not previous_values:
                continue
            busy_pct = _cpu_busy_pct(previous_values, current_values)
            if key == "cpu":
                cpu_all_pct = busy_pct
            elif busy_pct > busiest_core_pct:
                busiest_core_pct = busy_pct
    cpu_state_file.write_text(
        "\n".join(f"{key} {' '.join(str(v) for v in values)}" for key, values in current_cpu_lines.items()) + "\n"
    )

    mem_total_kb = mem_avail_kb = swap_total_kb = swap_free_kb = 0
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemTotal:"):
            mem_total_kb = int(line.split()[1])
        elif line.startswith("MemAvailable:"):
            mem_avail_kb = int(line.split()[1])
        elif line.startswith("SwapTotal:"):
            swap_total_kb = int(line.split()[1])
        elif line.startswith("SwapFree:"):
            swap_free_kb = int(line.split()[1])

    disk_avail_kb = 0
    backing_device = ""
    df_lines = subprocess.run(["df", "--output=avail,source", str(streams_root)],
                               capture_output=True, text=True).stdout.splitlines()
    if len(df_lines) >= 2:
        avail, source = df_lines[-1].split()
        disk_avail_kb = int(avail)
        backing_device = Path(source).name

    disk_sectors_read = disk_sectors_written = disk_io_ms = disk_ios = 0
    if backing_device:
        for line in Path("/proc/diskstats").read_text().splitlines():
            fields = line.split()
            if len(fields) > 13 and fields[2] == backing_device:
                disk_sectors_read = int(fields[5])
                disk_sectors_written = int(fields[9])
                disk_io_ms = int(fields[12])
                disk_ios = int(fields[3]) + int(fields[7])
                break

    row = ",".join(str(value) for value in (
        now, stream_count, stalled_count, load_1min, nproc, round(busiest_core_pct),
        mem_total_kb, mem_avail_kb, swap_total_kb, swap_free_kb, disk_avail_kb,
        disk_sectors_read, disk_sectors_written, disk_io_ms, disk_ios,
        _read_net_stat(iface, "rx_bytes"), _read_net_stat(iface, "tx_bytes"),
        _read_net_stat(iface, "rx_dropped"), _read_net_stat(iface, "tx_dropped"),
        _read_net_stat(iface, "rx_errors"), _read_net_stat(iface, "tx_errors"),
        round(cpu_all_pct),
    ))
    with history_file.open("a") as f:
        f.write(row + "\n")

    all_lines = history_file.read_text().splitlines()
    if len(all_lines) - 1 > MAX_HISTORY_ROWS:
        history_file.write_text("\n".join([all_lines[0]] + all_lines[-MAX_HISTORY_ROWS:]) + "\n")


_STATUS_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Live Flow - Capacity Test</title>
<style>
  :root {{
    --bg: #0f1115; --panel: #171a21; --line: #262b36;
    --text: #e6e9ef; --muted: #9aa3b2; --accent: #4ade80;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding: 40px 24px;
  }}
  .wrap {{ max-width: 720px; margin: 0 auto; }}
  .crumb {{ font-size: 13px; margin-bottom: 8px; }}
  .crumb a {{ color: var(--muted); text-decoration: none; }}
  .crumb a:hover {{ color: var(--text); text-decoration: underline; }}
  .crumb .sep {{ color: #6b7280; margin: 0 6px; }}
  .crumb .here {{ color: var(--muted); }}
  h1 {{ font-size: 26px; margin: 0 0 24px; font-weight: 600; }}
  .panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
           padding: 8px 22px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 15px; }}
  th {{ text-align: left; color: var(--muted); font-weight: 500; font-size: 12px;
       text-transform: uppercase; letter-spacing: .04em; padding: 14px 0 10px; }}
  td {{ padding: 14px 0; border-top: 1px solid var(--line); vertical-align: middle; }}
  td:nth-child(2) {{ font-size: 13px; }}
  td:nth-child(3) {{ color: var(--muted); }}
  .ip {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--muted); }}
  .dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%;
         background: var(--accent); margin-right: 10px;
         box-shadow: 0 0 0 3px rgba(74,222,128,.15); }}
  .watch {{ color: var(--bg); background: var(--accent); text-decoration: none;
           font-weight: 600; font-size: 13px; padding: 7px 14px; border-radius: 7px;
           display: inline-block; float: right; }}
  .watch:hover {{ opacity: .85; }}
  .empty {{ color: var(--muted); margin: 10px 0; }}
  .foot {{ color: var(--muted); font-size: 12px; margin-top: 18px; }}
</style>
</head>
<body>
  <div class="wrap">
    <div class="crumb"><a href="/">Live Flow</a><span class="sep">/</span><span class="here">Active Streams</span></div>
    <h1>{headline}</h1>
    <div class="panel">{body}</div>
    <div class="foot">Updates automatically &middot; last refreshed {refreshed} IST</div>
  </div>
</body>
</html>
"""


def _rtmp_peer_ip(port: int) -> str:
    output = subprocess.run(["ss", "-Htn", "state", "established", f"( sport = :{port} )"],
                             capture_output=True, text=True).stdout
    for line in output.splitlines():
        columns = line.split()
        if not columns:
            continue
        peer_address = columns[-1]
        ip = peer_address.rsplit(":", 1)[0] if ":" in peer_address else peer_address
        if ip and not ip.startswith("127."):
            return ip
    return ""


def status_page() -> StatusPageResult:
    streams_root = Path(STREAMS_ROOT)
    status_dir = Path(STATUS_DIR)
    status_dir.mkdir(parents=True, exist_ok=True)

    now = time.time()
    entries = []
    if streams_root.is_dir():
        for stream_dir in streams_root.iterdir():
            index_file = stream_dir / "index"
            if not stream_dir.is_dir() or not index_file.exists():
                continue
            try:
                port_index = int(index_file.read_text().strip())
            except ValueError:
                continue
            entries.append((index_file.stat().st_mtime, stream_dir.name, port_index))
    entries.sort()

    rows = []
    for position, (created_at, playback_id, port_index) in enumerate(entries, 1):
        minutes_live = int((now - created_at) / 60)
        if minutes_live < 1:
            live_for = "just started"
        elif minutes_live == 1:
            live_for = "1 minute"
        else:
            live_for = f"{minutes_live} minutes"

        peer_ip = _rtmp_peer_ip(1935 + port_index)
        broadcaster_name_file = status_dir / "broadcasters" / playback_id
        broadcaster_name = broadcaster_name_file.read_text().strip() if broadcaster_name_file.exists() else ""
        if broadcaster_name and peer_ip:
            broadcaster_cell = f'{broadcaster_name}<br><span class="ip">{peer_ip}</span>'
        elif peer_ip:
            broadcaster_cell = f'<span class="ip">{peer_ip}</span>'
        else:
            broadcaster_cell = "&mdash;"

        rows.append(f"""
        <tr>
          <td><span class="dot"></span>Stream {position}</td>
          <td>{broadcaster_cell}</td>
          <td>{live_for}</td>
          <td><a class="watch" href="http://{SERVER_IP}/player/?id={playback_id}" target="_blank" rel="noopener">Watch &rarr;</a></td>
        </tr>""")

    stream_count = len(entries)
    if stream_count == 0:
        headline = "No streams are live right now"
        body = '<p class="empty">Nothing is currently broadcasting. This page updates on its own once one starts.</p>'
    else:
        noun = "stream" if stream_count == 1 else "streams"
        headline = f"{stream_count} {noun} live right now"
        body = f'<table><tr><th>Stream</th><th>Broadcaster</th><th>Running for</th><th></th></tr>{"".join(rows)}</table>'

    html = _STATUS_PAGE_TEMPLATE.format(headline=headline, body=body, refreshed=time.strftime("%d %b, %I:%M %p"))
    output_file = status_dir / "index.html.tmp"
    output_file.write_text(html)
    final_path = status_dir / "index.html"
    output_file.rename(final_path)

    return StatusPageResult(stream_count=stream_count, output_path=str(final_path))


_TIMER_UNIT_TEMPLATE = """[Unit]
Description=Run {name} every {seconds} seconds

[Timer]
OnBootSec={boot_delay}
OnUnitActiveSec={seconds}
AccuracySec={accuracy}

[Install]
WantedBy=timers.target
"""

_SERVICE_UNIT_TEMPLATE = """[Unit]
Description={description}

[Service]
Type=oneshot
User={user}
Group={group}
WorkingDirectory={repo_dir}
ExecStart=/usr/bin/python3 {repo_dir}/dashboard.py {command}
"""


def install() -> StatusPageResult:
    docker_available = subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    docker_prefix = ["docker"] if docker_available else ["sudo", "-n", "docker"]

    status_dir = Path(STATUS_DIR)
    subprocess.run(["sudo", "mkdir", "-p", str(status_dir)])
    subprocess.run(["sudo", "chown", f"{os.getuid()}:{os.getgid()}", str(status_dir)])

    print("starting the shared edge server (/streams/ and /dashboard/ routes)...")
    env = {**os.environ, "STREAMS_ROOT": STREAMS_ROOT, "STATUS_DIR": str(status_dir)}
    subprocess.run([*docker_prefix, "compose", "-f", "docker-compose.yml", "up", "-d", "--force-recreate"],
                    cwd=REPO_ROOT, env=env)

    print("installing the regeneration timers...")
    user = getpass.getuser()
    group = grp.getgrgid(os.getgid()).gr_name
    repo_dir = str(REPO_ROOT)

    unit_files = {
        "lf-status-gen.service": _SERVICE_UNIT_TEMPLATE.format(
            description="Regenerate the live-flow status page", user=user, group=group,
            repo_dir=repo_dir, command="status-page"),
        "lf-status-gen.timer": _TIMER_UNIT_TEMPLATE.format(name="lf-status-gen", seconds=60, boot_delay=15, accuracy=5),
        "lf-sample.service": _SERVICE_UNIT_TEMPLATE.format(
            description="Record one row of capacity history for the /dashboard/ graph",
            user=user, group=group, repo_dir=repo_dir, command="sample"),
        "lf-sample.timer": _TIMER_UNIT_TEMPLATE.format(name="lf-sample", seconds=5, boot_delay=5, accuracy=1),
    }
    for unit_name, unit_content in unit_files.items():
        subprocess.run(["sudo", "tee", f"/etc/systemd/system/{unit_name}"], input=unit_content, text=True,
                        capture_output=True)

    subprocess.run(["sudo", "systemctl", "daemon-reload"])
    subprocess.run(["sudo", "systemctl", "enable", "--now", "lf-status-gen.timer", "lf-sample.timer"])

    print("generating the page once now...")
    sample()
    return status_page()


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "sample":
        sample()
    elif action == "status-page":
        result = status_page()
        print(f"{result.stream_count} stream(s) live -> {result.output_path}")
    elif action == "install":
        result = install()
        print(f"\nDone. {result.stream_count} stream(s) live.")
    else:
        print("usage: dashboard.py [sample|status-page|install]", file=sys.stderr)
        sys.exit(1)
