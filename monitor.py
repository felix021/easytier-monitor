#!/usr/bin/env python3
"""EasyTier health monitor — auto-restart on consecutive network failures."""

import argparse
import logging
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
    return p.parse_args(argv)


def get_instance_names(cli="easytier-cli"):
    cmd = [cli, "peer", "list"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
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
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


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
        return check_instance(cli, None, ping_timeout, ping_count, max_workers)
    return all(
        check_instance(cli, name, ping_timeout, ping_count, max_workers)
        for name in instance_names
    )


def restart_service(restart_cmd):
    log.info(f"Executing: {restart_cmd}")
    subprocess.run(restart_cmd, shell=True, check=True, capture_output=True, text=True)
    log.info("Restart done")


def ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def run(args):
    instance_names = args.instance_names
    if not instance_names:
        instance_names = get_instance_names(args.cli)
    inst_info = f"instances={instance_names}" if instance_names else "instances=none(found)"
    log.info(f"EasyTier monitor started — "
             f"interval={args.interval}s threshold={args.threshold} "
             f"{inst_info} restart_cmd='{args.restart_cmd}'")
    if not instance_names:
        log.info("No instances found, exiting")
        return
    failures = 0

    while True:
        ok = check_network(
            cli=args.cli, instance_names=instance_names,
            ping_timeout=args.ping_timeout, ping_count=args.ping_count,
        )
        if ok:
            if failures:
                log.info(f"Recovered after {failures} failures")
            failures = 0
        else:
            failures += 1
            log.info(f"Network check failed ({failures}/{args.threshold})")
            if failures >= args.threshold:
                try:
                    restart_service(args.restart_cmd)
                except subprocess.CalledProcessError as e:
                    log.info(f"Restart failed: {e.stderr or e}")
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
