#!/usr/bin/env python3
"""EasyTier health monitor — auto-restart on consecutive network failures."""

import argparse
import logging
import os
import platform
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

IS_WINDOWS = platform.system() == "Windows"

log = logging.getLogger("easytier-monitor")


def setup_logging(log_file=None):
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    for h in handlers:
        h.setFormatter(fmt)
        log.addHandler(h)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="EasyTier health monitor")
    p.add_argument("--interval", type=int, default=5, help="Check interval in seconds (default: 5)")
    p.add_argument("--threshold", type=int, default=3, help="Consecutive failures before restart (default: 3)")
    p.add_argument("--ping-timeout", type=int, default=2, help="Ping timeout per host in seconds (default: 2)")
    p.add_argument("--ping-count", type=int, default=1, help="Ping packet count per host (default: 1)")
    p.add_argument("--cooldown", type=int, default=30, help="Seconds to wait after restart (default: 30)")
    p.add_argument("--restart-cmd", default="systemctl restart easytier",
                   help="Shell command to restart EasyTier (default: 'systemctl restart easytier')")
    p.add_argument("--log-file", help="Write logs to this file in addition to stdout")
    p.add_argument("--cli", default="easytier-cli", help="Path to easytier-cli (default: easytier-cli)")
    p.add_argument("--instance-name", dest="instance_names", action="append", default=[],
                   help="Instance name to monitor (repeatable, e.g. --instance-name net1 --instance-name net2). "
                        "Omit to check all instances without filter.")
    p.add_argument("--fd-unit", default="",
                   help="systemd unit whose MainPID's fd usage is checked against its soft limit "
                        "(e.g. easytier.service). Empty (default) disables the check. Linux only.")
    p.add_argument("--fd-threshold", type=int, default=70,
                   help="fd usage percentage of the soft limit treated as failure (default: 70)")
    return p.parse_args(argv)


def get_instance_names(cli="easytier-cli"):
    cmd = [cli, "peer", "list"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return []
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    names = []
    for line in r.stdout.splitlines():
        m = re.match(r"==\s+(\S+)\s+", line)
        if m:
            names.append(m.group(1))
    return names


def get_peer_ips(cli="easytier-cli", instance_name=None):
    cmd = [cli]
    if instance_name:
        cmd += ["-n", instance_name]
    cmd += ["peer", "list"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return []
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    ips = []
    for line in r.stdout.splitlines():
        m = re.search(r"\|\s*(\d+\.\d+\.\d+\.\d+)/\d+\s*\|", line)
        if m:
            ip = m.group(1)
            if "Local" in line:
                continue
            ips.append(ip)
    return ips


def _ping_cmd(target, timeout=2, count=1):
    if IS_WINDOWS:
        return ["ping", "-n", str(count), "-w", str(timeout * 1000), target]
    return ["ping", "-c", str(count), "-W", str(timeout), target]


def check_ping(target, timeout=2, count=1):
    try:
        cmd = _ping_cmd(target, timeout, count)
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            proc.communicate(timeout=(timeout * count + 5))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return False
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def get_service_pid(unit="easytier.service"):
    """Return the MainPID of a systemd unit, or None if unavailable (non-systemd, stopped...).

    unit may be "docker:<container-name>" for containerized EasyTier — the
    container's host-side PID is returned, so /proc checks work from a monitor
    running on the host.
    """
    if IS_WINDOWS:
        return None
    if unit.startswith("docker:"):
        name = unit[len("docker:"):]
        if not name:
            return None
        try:
            r = subprocess.run(["docker", "inspect", "-f", "{{.State.Pid}}", name],
                               capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                return None
            return int(r.stdout.strip() or 0) or None
        except (FileNotFoundError, ValueError, subprocess.TimeoutExpired):
            return None
    try:
        r = subprocess.run(["systemctl", "show", unit, "--property=MainPID", "--value"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return None
        return int(r.stdout.strip() or 0) or None
    except (FileNotFoundError, ValueError, subprocess.TimeoutExpired):
        return None


_fd_unavailable_warned = False


def check_fd_usage(unit="", threshold_pct=70):
    """Check fd usage of the unit's main process against its soft RLIMIT_NOFILE.

    Returns (ok, message). Unavailable (no unit, Windows, pid/fd not readable)
    counts as ok — this check must not fail the health verdict on its own errors.
    Catches exhaustion of EasyTier's fd pool: with KCP proxy enabled new TCP
    flows need a fresh socket on the receiving side, and EMFILE there kills
    connections while ICMP/peer checks stay green.
    """
    global _fd_unavailable_warned
    if IS_WINDOWS or not unit:
        return True, ""
    pid = get_service_pid(unit)
    if not pid:
        return True, ""
    try:
        fds = len(os.listdir(f"/proc/{pid}/fd"))
        with open(f"/proc/{pid}/limits") as f:
            limits = f.read()
    except OSError as e:
        # Configured but unreadable (e.g. monitor not running as root) would
        # otherwise silently no-op forever — warn once so it gets noticed.
        if not _fd_unavailable_warned:
            _fd_unavailable_warned = True
            log.warning(f"fd check for {unit} (pid {pid}) unavailable: {e}; skipping")
        return True, ""
    soft = 0
    for line in limits.splitlines():
        if line.startswith("Max open files"):
            parts = line.split()
            if len(parts) >= 4:
                soft = int(parts[3])
            break
    if not soft:
        return True, ""
    if fds >= soft * threshold_pct / 100.0:
        return False, f"{unit} pid {pid} fd usage {fds}/{soft} ({fds * 100 // soft}% >= {threshold_pct}%)"
    return True, ""


def check_instance(cli, instance_name, ping_timeout, ping_count, max_workers):
    peers = get_peer_ips(cli=cli, instance_name=instance_name)
    if not peers:
        return False
    with ThreadPoolExecutor(max_workers=min(len(peers), max_workers)) as pool:
        futures = {pool.submit(check_ping, ip, ping_timeout, ping_count): ip for ip in peers}
        for future in as_completed(futures):
            if future.result():
                for f in futures:
                    f.cancel()
                return True
    return False


def check_network(cli="easytier-cli", instance_names=None, ping_timeout=2, ping_count=1, max_workers=16):
    if not instance_names:
        ok = check_instance(cli, None, ping_timeout, ping_count, max_workers)
        return ok, [] if ok else [None]
    failed = [name for name in instance_names
              if not check_instance(cli, name, ping_timeout, ping_count, max_workers)]
    return not failed, failed


def _discover_instances(cli):
    names = get_instance_names(cli)
    if names:
        return names
    if get_peer_ips(cli):
        return [None]
    return []


def _display_name(n):
    return n or "(default)"


def restart_service(restart_cmd):
    log.info(f"Executing: {restart_cmd}")
    subprocess.run(restart_cmd, shell=True, check=True, capture_output=True, text=True)
    log.info("Restart done")


def ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def run(args):
    auto_discover = not args.instance_names
    if auto_discover:
        instance_names = _discover_instances(args.cli)
    else:
        instance_names = list(args.instance_names)

    inst_info = (f"instances={[_display_name(n) for n in instance_names]}"
                 if instance_names else "no instances found yet")
    fd_info = f" fd_unit={args.fd_unit}@{args.fd_threshold}%" if args.fd_unit else ""
    log.info(f"EasyTier monitor started — "
             f"interval={args.interval}s threshold={args.threshold} "
             f"{inst_info}{fd_info} restart_cmd='{args.restart_cmd}'")

    # Startup race: easytier.service may still be initializing (RPC portal not
    # ready), so an empty first discovery must not exit the daemon — retry.
    # A clean exit here is unrecoverable under Restart=on-failure.
    retried = False
    while not instance_names:
        retried = True
        log.warning("No instances discovered (easytier RPC not ready?), retrying...")
        time.sleep(args.interval)
        instance_names = _discover_instances(args.cli)
    if retried:
        log.info(f"Instances discovered: {[_display_name(n) for n in instance_names]}")

    failures = 0

    while True:
        if auto_discover:
            refreshed = _discover_instances(args.cli)
            if refreshed != instance_names:
                added = [n for n in refreshed if n not in instance_names]
                removed = [n for n in instance_names if n not in refreshed]
                if added:
                    log.info(f"Instances added: {[_display_name(n) for n in added]}")
                if removed:
                    log.info(f"Instances removed: {[_display_name(n) for n in removed]}")
                instance_names = refreshed
            if not instance_names:
                # Discovery returned nothing while running — normally that means
                # easytier-cli cannot reach the RPC portal, i.e. easytier is
                # hung/deadlocked (process alive, RPC dead). Probe via check_network
                # so this counts as a failure and can trigger a restart, instead of
                # waiting forever while the network stays down.
                log.warning("No instances discovered (cli/RPC unreachable?), probing as failure")
                instance_names = [None]

        ok, failed_instances = check_network(
            cli=args.cli, instance_names=instance_names,
            ping_timeout=args.ping_timeout, ping_count=args.ping_count,
        )
        fd_ok, fd_msg = check_fd_usage(args.fd_unit, args.fd_threshold)
        if not fd_ok:
            ok = False
            log.warning(f"FD check failed: {fd_msg}")
        if ok:
            if failures:
                log.warning(f"Network recovered after {failures} consecutive failures")
            failures = 0
        else:
            failures += 1
            failed_display = [_display_name(n) for n in failed_instances]
            log.warning(f"Network check failed ({failures}/{args.threshold}) — "
                        f"unreachable: {failed_display}")
            if failures >= args.threshold:
                log.error(f"Restarting EasyTier after {failures} consecutive failures — "
                          f"unreachable: {failed_display}")
                try:
                    restart_service(args.restart_cmd)
                except subprocess.CalledProcessError as e:
                    log.error(f"Restart failed: {e.stderr or e}")
                    failures = 0
                    continue
                failures = 0
                log.info(f"Cooling down {args.cooldown}s...")
                time.sleep(args.cooldown)
                continue

        time.sleep(args.interval)


if __name__ == "__main__":
    args = parse_args()
    setup_logging(args.log_file)
    run(args)
