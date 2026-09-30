import subprocess
import time
import unittest
from unittest.mock import patch, MagicMock, call
from concurrent.futures import ThreadPoolExecutor
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
import monitor


CLI_OUTPUT = """| ipv4            | hostname                       | cost     | lat(ms) | loss  | rx        | tx        | tunnel   | NAT              | version        |
|-----------------|--------------------------------|----------|---------|-------|-----------|-----------|----------|------------------|----------------|
| 10.196.0.11/24  | pve-ubuntu                     | Local    | -       | -     | -         | -         | -        | Unknown          | 2.6.4-8428a89d |
|                 | PublicServer_butnoice-vps-wlcb | p2p      | 33.73   | 0.0%  | 4.40 MB   | 5.51 MB   | tcp      | PortRestricted   | 2.5.0-88a45d11 |
| 10.196.0.4/24   | MeMini                         | p2p      | 37.13   | 10.0% | 4.47 MB   | 2.80 MB   | udp,udp6 | PortRestricted   | 2.6.4-8428a89d |
| 10.196.0.169/24 | qcloud-sh                      | p2p      | 9.57    | 3.0%  | 327.61 MB | 442.09 MB | txt-tcp  | Unknown          | 2.6.4-8428a89d |
"""

CLI_OUTPUT_ONLY_LOCAL = """| ipv4            | hostname                       | cost     | lat(ms) | loss  | rx        | tx        | tunnel   | NAT              | version        |
|-----------------|--------------------------------|----------|---------|-------|-----------|-----------|----------|------------------|----------------|
| 10.196.0.11/24  | pve-ubuntu                     | Local    | -       | -     | -         | -         | -        | Unknown          | 2.6.4-8428a89d |
"""


class TestParseArgs(unittest.TestCase):
    def test_defaults(self):
        args = monitor.parse_args([])
        self.assertEqual(args.interval, 5)
        self.assertEqual(args.threshold, 3)
        self.assertEqual(args.ping_timeout, 2)
        self.assertEqual(args.ping_count, 1)
        self.assertEqual(args.cooldown, 30)
        self.assertEqual(args.restart_cmd, "systemctl restart easytier")
        self.assertEqual(args.cli, "easytier-cli")
        self.assertEqual(args.instance_names, [])

    def test_single_instance_name(self):
        args = monitor.parse_args(["--instance-name", "net1"])
        self.assertEqual(args.instance_names, ["net1"])

    def test_multiple_instance_names(self):
        args = monitor.parse_args(["--instance-name", "net1", "--instance-name", "net2"])
        self.assertEqual(args.instance_names, ["net1", "net2"])

    def test_custom_restart_cmd(self):
        args = monitor.parse_args(["--restart-cmd", "docker restart easytier"])
        self.assertEqual(args.restart_cmd, "docker restart easytier")

    def test_custom_cli(self):
        args = monitor.parse_args(["--cli", "/usr/local/bin/easytier-cli"])
        self.assertEqual(args.cli, "/usr/local/bin/easytier-cli")


class TestGetPeerIPs(unittest.TestCase):
    @patch("subprocess.run")
    def test_parses_peer_list(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=CLI_OUTPUT)
        ips = monitor.get_peer_ips()
        self.assertEqual(ips, ["10.196.0.4", "10.196.0.169"])

    @patch("subprocess.run")
    def test_excludes_local(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=CLI_OUTPUT)
        ips = monitor.get_peer_ips()
        self.assertNotIn("10.196.0.11", ips)

    @patch("subprocess.run")
    def test_excludes_empty_ipv4(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=CLI_OUTPUT)
        ips = monitor.get_peer_ips()
        self.assertNotIn("", ips)

    @patch("subprocess.run")
    def test_only_local_no_peers(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=CLI_OUTPUT_ONLY_LOCAL)
        ips = monitor.get_peer_ips()
        self.assertEqual(ips, [])

    @patch("subprocess.run")
    def test_cli_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        ips = monitor.get_peer_ips()
        self.assertEqual(ips, [])

    @patch("subprocess.run")
    def test_no_instance_filter(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=CLI_OUTPUT)
        monitor.get_peer_ips(cli="/custom/cli")
        mock_run.assert_called_once_with(
            ["/custom/cli", "peer", "list"], capture_output=True, text=True, timeout=10
        )

    @patch("subprocess.run")
    def test_with_instance_name(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=CLI_OUTPUT)
        monitor.get_peer_ips(cli="easytier-cli", instance_name="net1")
        mock_run.assert_called_once_with(
            ["easytier-cli", "-n", "net1", "peer", "list"], capture_output=True, text=True, timeout=10
        )

    @patch("subprocess.run")
    def test_instance_name_none_no_filter(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=CLI_OUTPUT)
        monitor.get_peer_ips(cli="easytier-cli", instance_name=None)
        mock_run.assert_called_once_with(
            ["easytier-cli", "peer", "list"], capture_output=True, text=True, timeout=10
        )


class TestCheckPing(unittest.TestCase):
    @patch("subprocess.Popen")
    def test_ping_success(self, mock_popen):
        mock_proc = MagicMock(returncode=0)
        mock_proc.wait.return_value = None
        mock_popen.return_value = mock_proc
        self.assertTrue(monitor.check_ping("10.196.0.169", timeout=2, count=1))

    @patch("subprocess.Popen")
    def test_ping_failure(self, mock_popen):
        mock_proc = MagicMock(returncode=1)
        mock_proc.wait.return_value = None
        mock_popen.return_value = mock_proc
        self.assertFalse(monitor.check_ping("10.196.0.169", timeout=2, count=1))

    @patch("subprocess.Popen")
    def test_ping_timeout(self, mock_popen):
        mock_proc = MagicMock(returncode=2)
        mock_proc.wait.return_value = None
        mock_popen.return_value = mock_proc
        self.assertFalse(monitor.check_ping("10.196.0.169", timeout=2, count=1))


class TestPingCmd(unittest.TestCase):
    @patch.object(monitor, "IS_WINDOWS", False)
    def test_linux_ping_cmd(self):
        cmd = monitor._ping_cmd("10.0.0.1", timeout=2, count=3)
        self.assertEqual(cmd, ["ping", "-c", "3", "-W", "2", "10.0.0.1"])

    @patch.object(monitor, "IS_WINDOWS", True)
    def test_windows_ping_cmd(self):
        cmd = monitor._ping_cmd("10.0.0.1", timeout=2, count=3)
        self.assertEqual(cmd, ["ping", "-n", "3", "-w", "2000", "10.0.0.1"])


class TestCheckInstance(unittest.TestCase):
    @patch.object(monitor, "check_ping")
    @patch.object(monitor, "get_peer_ips")
    def test_any_peer_ok(self, mock_peers, mock_ping):
        mock_peers.return_value = ["10.196.0.4", "10.196.0.169"]
        mock_ping.side_effect = [False, True]
        self.assertTrue(monitor.check_instance("cli", None, 2, 1, 4))

    @patch.object(monitor, "check_ping")
    @patch.object(monitor, "get_peer_ips")
    def test_all_fail(self, mock_peers, mock_ping):
        mock_peers.return_value = ["10.196.0.4", "10.196.0.169"]
        mock_ping.return_value = False
        self.assertFalse(monitor.check_instance("cli", None, 2, 1, 4))

    @patch.object(monitor, "check_ping")
    @patch.object(monitor, "get_peer_ips")
    def test_no_peers(self, mock_peers, mock_ping):
        mock_peers.return_value = []
        self.assertFalse(monitor.check_instance("cli", "net1", 2, 1, 4))
        mock_ping.assert_not_called()

    @patch.object(monitor, "check_ping")
    @patch.object(monitor, "get_peer_ips")
    def test_passes_instance_name(self, mock_peers, mock_ping):
        mock_peers.return_value = ["10.196.0.169"]
        mock_ping.return_value = True
        monitor.check_instance("cli", "net1", 2, 1, 4)
        mock_peers.assert_called_once_with(cli="cli", instance_name="net1")


class TestCheckNetwork(unittest.TestCase):
    @patch.object(monitor, "check_instance")
    def test_no_instance_names_ok(self, mock_ci):
        mock_ci.return_value = True
        result = monitor.check_network(cli="cli", instance_names=None, ping_timeout=2, ping_count=1)
        self.assertEqual(result, (True, []))
        mock_ci.assert_called_once_with("cli", None, 2, 1, 16)

    @patch.object(monitor, "check_instance")
    def test_no_instance_names_fail(self, mock_ci):
        mock_ci.return_value = False
        result = monitor.check_network(cli="cli", instance_names=None, ping_timeout=2, ping_count=1)
        self.assertEqual(result, (False, [None]))

    @patch.object(monitor, "check_instance")
    def test_empty_list_ok(self, mock_ci):
        mock_ci.return_value = True
        result = monitor.check_network(cli="cli", instance_names=[], ping_timeout=2, ping_count=1)
        self.assertEqual(result, (True, []))
        mock_ci.assert_called_once_with("cli", None, 2, 1, 16)

    @patch.object(monitor, "check_instance")
    def test_single_instance_ok(self, mock_ci):
        mock_ci.return_value = True
        result = monitor.check_network(cli="cli", instance_names=["net1"], ping_timeout=2, ping_count=1)
        self.assertEqual(result, (True, []))
        mock_ci.assert_called_once_with("cli", "net1", 2, 1, 16)

    @patch.object(monitor, "check_instance")
    def test_single_instance_fail(self, mock_ci):
        mock_ci.return_value = False
        result = monitor.check_network(cli="cli", instance_names=["net1"], ping_timeout=2, ping_count=1)
        self.assertEqual(result, (False, ["net1"]))

    @patch.object(monitor, "check_instance")
    def test_multiple_instances_all_ok(self, mock_ci):
        mock_ci.return_value = True
        result = monitor.check_network(cli="cli", instance_names=["net1", "net2"], ping_timeout=2, ping_count=1)
        self.assertEqual(result, (True, []))
        self.assertEqual(mock_ci.call_count, 2)

    @patch.object(monitor, "check_instance")
    def test_multiple_instances_one_fails(self, mock_ci):
        mock_ci.side_effect = [True, False]
        result = monitor.check_network(cli="cli", instance_names=["net1", "net2"], ping_timeout=2, ping_count=1)
        self.assertEqual(result, (False, ["net2"]))

    @patch.object(monitor, "check_instance")
    def test_multiple_instances_all_fail(self, mock_ci):
        mock_ci.return_value = False
        result = monitor.check_network(cli="cli", instance_names=["net1", "net2"], ping_timeout=2, ping_count=1)
        self.assertEqual(result, (False, ["net1", "net2"]))


class TestRestartService(unittest.TestCase):
    @patch("subprocess.run")
    def test_systemd_restart(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        monitor.restart_service("systemctl restart easytier")
        mock_run.assert_called_once_with(
            "systemctl restart easytier", shell=True, check=True, capture_output=True, text=True
        )

    @patch("subprocess.run")
    def test_docker_restart(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        monitor.restart_service("docker restart easytier")
        mock_run.assert_called_once_with(
            "docker restart easytier", shell=True, check=True, capture_output=True, text=True
        )

    @patch("subprocess.run")
    def test_restart_failure_raises(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(1, "bash", stderr="error")
        with self.assertRaises(subprocess.CalledProcessError):
            monitor.restart_service("systemctl restart easytier")


class TestMonitorLoop(unittest.TestCase):
    @patch("builtins.print")
    @patch("time.sleep")
    @patch.object(monitor, "restart_service")
    @patch.object(monitor, "check_network")
    @patch.object(monitor, "_discover_instances")
    def test_no_restart_on_success(self, mock_discover, mock_net, mock_restart, mock_sleep, mock_print):
        mock_discover.return_value = [None]
        mock_net.return_value = (True, [])
        args = monitor.parse_args(["--threshold", "3"])
        call_count = [0]

        def sleep_side_effect(_):
            call_count[0] += 1
            if call_count[0] >= 3:
                raise KeyboardInterrupt

        mock_sleep.side_effect = sleep_side_effect
        try:
            monitor.run(args)
        except KeyboardInterrupt:
            pass
        mock_restart.assert_not_called()

    @patch("builtins.print")
    @patch("time.sleep")
    @patch.object(monitor, "restart_service")
    @patch.object(monitor, "check_network")
    @patch.object(monitor, "_discover_instances")
    def test_restart_after_threshold(self, mock_discover, mock_net, mock_restart, mock_sleep, mock_print):
        mock_discover.return_value = [None]
        mock_net.return_value = (False, [None])
        mock_restart.return_value = None
        args = monitor.parse_args(["--threshold", "3"])
        call_count = [0]

        def sleep_side_effect(_):
            call_count[0] += 1
            if call_count[0] >= 5:
                raise KeyboardInterrupt

        mock_sleep.side_effect = sleep_side_effect
        try:
            monitor.run(args)
        except KeyboardInterrupt:
            pass
        self.assertGreaterEqual(mock_restart.call_count, 1)

    @patch("builtins.print")
    @patch("time.sleep")
    @patch.object(monitor, "restart_service")
    @patch.object(monitor, "check_network")
    @patch.object(monitor, "_discover_instances")
    def test_failure_counter_resets_on_success(self, mock_discover, mock_net, mock_restart, mock_sleep, mock_print):
        mock_discover.return_value = [None]
        results = [(False, [None]), (False, [None]), (True, []), (False, [None]), (False, [None])]
        mock_net.side_effect = results
        mock_restart.return_value = None
        args = monitor.parse_args(["--threshold", "3"])
        call_count = [0]

        def sleep_side_effect(_):
            call_count[0] += 1
            if call_count[0] >= 5:
                raise KeyboardInterrupt

        mock_sleep.side_effect = sleep_side_effect
        try:
            monitor.run(args)
        except KeyboardInterrupt:
            pass
        mock_restart.assert_not_called()

    @patch("builtins.print")
    @patch("time.sleep")
    @patch.object(monitor, "restart_service")
    @patch.object(monitor, "check_network")
    @patch.object(monitor, "_discover_instances")
    def test_restart_recovers(self, mock_discover, mock_net, mock_restart, mock_sleep, mock_print):
        mock_discover.return_value = [None]
        results = iter([(False, [None]), (False, [None]), (False, [None]), (True, []), (True, [])])
        mock_net.side_effect = lambda *a, **kw: next(results)
        mock_restart.return_value = None
        args = monitor.parse_args(["--threshold", "3", "--cooldown", "5"])
        call_count = [0]

        def sleep_side_effect(secs):
            call_count[0] += 1
            if call_count[0] >= 4:
                raise KeyboardInterrupt

        mock_sleep.side_effect = sleep_side_effect
        try:
            monitor.run(args)
        except KeyboardInterrupt:
            pass
        self.assertEqual(mock_restart.call_count, 1)

    @patch("builtins.print")
    @patch("time.sleep")
    @patch.object(monitor, "restart_service")
    @patch.object(monitor, "check_network")
    def test_passes_instance_names_to_check_network(self, mock_net, mock_restart, mock_sleep, mock_print):
        mock_net.return_value = (True, [])
        args = monitor.parse_args(["--instance-name", "net1", "--instance-name", "net2"])
        call_count = [0]

        def sleep_side_effect(_):
            call_count[0] += 1
            if call_count[0] >= 1:
                raise KeyboardInterrupt

        mock_sleep.side_effect = sleep_side_effect
        try:
            monitor.run(args)
        except KeyboardInterrupt:
            pass
        mock_net.assert_called_once()
        _, kwargs = mock_net.call_args
        self.assertEqual(kwargs["instance_names"], ["net1", "net2"])

    @patch("builtins.print")
    @patch("time.sleep")
    @patch.object(monitor, "restart_service")
    @patch.object(monitor, "check_network")
    @patch.object(monitor, "_discover_instances")
    def test_refreshes_instances_when_one_removed(self, mock_discover, mock_net, mock_restart, mock_sleep, mock_print):
        mock_discover.side_effect = [["net1", "net2"], ["net1"], ["net1"], ["net1"]]
        mock_net.return_value = (True, [])
        args = monitor.parse_args(["--threshold", "3"])
        call_count = [0]

        def sleep_side_effect(_):
            call_count[0] += 1
            if call_count[0] >= 3:
                raise KeyboardInterrupt

        mock_sleep.side_effect = sleep_side_effect
        try:
            monitor.run(args)
        except KeyboardInterrupt:
            pass
        # After refresh, check_network should use ["net1"] only
        last_call = mock_net.call_args_list[-1]
        self.assertEqual(last_call[1]["instance_names"], ["net1"])
        mock_restart.assert_not_called()

    @patch("builtins.print")
    @patch("time.sleep")
    @patch.object(monitor, "restart_service")
    @patch.object(monitor, "check_network")
    @patch.object(monitor, "_discover_instances")
    def test_all_instances_removed_probes_as_failure(self, mock_discover, mock_net, mock_restart, mock_sleep, mock_print):
        # Discovery returning empty mid-run means cli/RPC is unreachable (easytier
        # hung). The monitor must still probe via check_network so it can restart.
        mock_discover.side_effect = [["net1"], [], []]
        mock_net.return_value = (False, [None])
        args = monitor.parse_args(["--threshold", "3"])
        call_count = [0]

        def sleep_side_effect(_):
            call_count[0] += 1
            if call_count[0] >= 2:
                raise KeyboardInterrupt

        mock_sleep.side_effect = sleep_side_effect
        try:
            monitor.run(args)
        except KeyboardInterrupt:
            pass
        mock_net.assert_called()
        mock_restart.assert_not_called()  # only 2 failures, below threshold 3


class TestGetServicePid(unittest.TestCase):
    @patch("subprocess.run")
    def test_parses_main_pid(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="1234\n")
        self.assertEqual(monitor.get_service_pid("easytier.service"), 1234)
        mock_run.assert_called_once_with(
            ["systemctl", "show", "easytier.service", "--property=MainPID", "--value"],
            capture_output=True, text=True, timeout=10,
        )

    @patch("subprocess.run")
    def test_zero_pid_means_none(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="0\n")
        self.assertIsNone(monitor.get_service_pid("easytier.service"))

    @patch("subprocess.run")
    def test_systemctl_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        self.assertIsNone(monitor.get_service_pid("easytier.service"))

    @patch.object(monitor, "IS_WINDOWS", True)
    def test_windows_skipped(self):
        self.assertIsNone(monitor.get_service_pid("easytier.service"))


class TestCheckFdUsage(unittest.TestCase):
    LIMITS = "Max open files            1024                524288              files\n"

    def _mock_proc(self, mock_open, mock_listdir, fds, limits=LIMITS):
        mock_listdir.return_value = ["fd"] * fds
        mock_open.return_value.__enter__.return_value.read.return_value = limits

    @patch("builtins.open")
    @patch("os.listdir")
    @patch.object(monitor, "get_service_pid")
    def test_over_threshold(self, mock_pid, mock_listdir, mock_open):
        mock_pid.return_value = 100
        self._mock_proc(mock_open, mock_listdir, 900)
        ok, msg = monitor.check_fd_usage("easytier.service", 70)
        self.assertFalse(ok)
        self.assertIn("900/1024", msg)

    @patch("builtins.open")
    @patch("os.listdir")
    @patch.object(monitor, "get_service_pid")
    def test_under_threshold(self, mock_pid, mock_listdir, mock_open):
        mock_pid.return_value = 100
        self._mock_proc(mock_open, mock_listdir, 100)
        ok, msg = monitor.check_fd_usage("easytier.service", 70)
        self.assertTrue(ok)
        self.assertEqual(msg, "")

    @patch("builtins.open")
    @patch("os.listdir")
    @patch.object(monitor, "get_service_pid")
    def test_exact_threshold_fails(self, mock_pid, mock_listdir, mock_open):
        mock_pid.return_value = 100
        # 70% of 1024 = 716.8 — at/above the threshold counts as failure
        self._mock_proc(mock_open, mock_listdir, 717)
        ok, _ = monitor.check_fd_usage("easytier.service", 70)
        self.assertFalse(ok)

    @patch("os.listdir")
    @patch.object(monitor, "get_service_pid")
    def test_pid_unavailable_skips(self, mock_pid, mock_listdir):
        mock_pid.return_value = None
        ok, msg = monitor.check_fd_usage("easytier.service", 70)
        self.assertTrue(ok)
        self.assertEqual(msg, "")
        mock_listdir.assert_not_called()

    @patch.object(monitor, "get_service_pid")
    def test_empty_unit_disables(self, mock_pid):
        ok, msg = monitor.check_fd_usage("", 70)
        self.assertTrue(ok)
        self.assertEqual(msg, "")
        mock_pid.assert_not_called()

    @patch("builtins.open")
    @patch("os.listdir")
    @patch.object(monitor, "get_service_pid")
    def test_missing_limits_line_skips(self, mock_pid, mock_listdir, mock_open):
        mock_pid.return_value = 100
        self._mock_proc(mock_open, mock_listdir, 900, limits="Max processes      228096  unlimited\n")
        ok, msg = monitor.check_fd_usage("easytier.service", 70)
        self.assertTrue(ok)
        self.assertEqual(msg, "")

    @patch("os.listdir")
    @patch.object(monitor, "get_service_pid")
    def test_fd_read_error_skips(self, mock_pid, mock_listdir):
        mock_pid.return_value = 100
        mock_listdir.side_effect = PermissionError("no access")
        ok, msg = monitor.check_fd_usage("easytier.service", 70)
        self.assertTrue(ok)
        self.assertEqual(msg, "")


class TestFdArgs(unittest.TestCase):
    def test_fd_defaults(self):
        args = monitor.parse_args([])
        self.assertEqual(args.fd_unit, "")
        self.assertEqual(args.fd_threshold, 70)

    def test_fd_options(self):
        args = monitor.parse_args(["--fd-unit", "easytier.service", "--fd-threshold", "80"])
        self.assertEqual(args.fd_unit, "easytier.service")
        self.assertEqual(args.fd_threshold, 80)


class TestStartupRetryAndFdLoop(unittest.TestCase):
    @patch("builtins.print")
    @patch("time.sleep")
    @patch.object(monitor, "restart_service")
    @patch.object(monitor, "check_network")
    @patch.object(monitor, "_discover_instances")
    def test_startup_retries_until_discovery_succeeds(self, mock_discover, mock_net, mock_restart, mock_sleep, mock_print):
        # Empty discovery at startup (easytier RPC not ready) must retry instead
        # of exiting the daemon. After success, mid-run refresh keeps succeeding.
        states = [[], [], [None]]
        mock_discover.side_effect = lambda *a, **kw: states.pop(0) if states else [None]
        mock_net.return_value = (True, [])
        args = monitor.parse_args([])
        call_count = [0]

        def sleep_side_effect(_):
            call_count[0] += 1
            if call_count[0] >= 3:
                raise KeyboardInterrupt

        mock_sleep.side_effect = sleep_side_effect
        try:
            monitor.run(args)
        except KeyboardInterrupt:
            pass
        self.assertEqual(mock_net.call_count, 1)

    @patch("builtins.print")
    @patch("time.sleep")
    @patch.object(monitor, "restart_service")
    @patch.object(monitor, "check_fd_usage")
    @patch.object(monitor, "check_network")
    @patch.object(monitor, "_discover_instances")
    def test_fd_exhaustion_triggers_restart(self, mock_discover, mock_net, mock_fd, mock_restart, mock_sleep, mock_print):
        # Network/ICMP stays healthy but fd usage is over threshold — the
        # KCP-proxy EMFILE failure mode; must still count toward a restart.
        mock_discover.return_value = [None]
        mock_net.return_value = (True, [])
        mock_fd.return_value = (False, "easytier.service pid 1069 fd usage 900/1024 (87% >= 70%)")
        args = monitor.parse_args(["--threshold", "3", "--fd-unit", "easytier.service"])
        call_count = [0]

        def sleep_side_effect(_):
            call_count[0] += 1
            if call_count[0] >= 5:
                raise KeyboardInterrupt

        mock_sleep.side_effect = sleep_side_effect
        try:
            monitor.run(args)
        except KeyboardInterrupt:
            pass
        self.assertEqual(mock_restart.call_count, 1)


if __name__ == "__main__":
    unittest.main()
