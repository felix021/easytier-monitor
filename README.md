# EasyTier Health Monitor

Auto-restart EasyTier on consecutive network failures.

## Usage

```bash
python monitor.py [options]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--interval` | 5 | Check interval in seconds |
| `--threshold` | 3 | Consecutive failures before restart |
| `--ping-timeout` | 2 | Ping timeout per host (seconds) |
| `--ping-count` | 1 | Ping packet count per host |
| `--cooldown` | 30 | Seconds to wait after restart |
| `--restart-cmd` | `systemctl restart easytier` | Shell command to restart EasyTier |
| `--cli` | `easytier-cli` | Path to easytier-cli |
| `--instance-name` | (auto-detect) | Instance name to monitor (repeatable) |

## How It Works

1. Auto-discovers all EasyTier instances via `easytier-cli peer list`
2. For each instance, extracts peer IPs and pings them in parallel
3. An instance is considered healthy if **any** peer responds
4. The overall check passes only if **all** instances are healthy
5. After `--threshold` consecutive failures, executes `--restart-cmd`

## Windows with WinSW

If EasyTier is installed as a Windows Service via [WinSW](https://github.com/winsw/winsw), use the WinSW executable to restart:

```bash
python monitor.py --restart-cmd "D:\programs\easytier\win-sw\WinSW-x64.exe restart easytier.xml"
```

WinSW manages the service lifecycle directly, no admin elevation needed (unlike `net stop/start`).

### Installing the monitor itself as a WinSW service

Create a WinSW XML config (e.g. `easytier-monitor.xml`) next to your `WinSW-x64.exe`:

```xml
<service>
  <id>easytier-monitor</id>
  <name>easytier-monitor</name>
  <description>EasyTier health monitor - auto-restart on consecutive failures</description>
  <dependsOn>easytier</dependsOn>
  <workingdirectory>D:\path\to\easytier-monitor\</workingdirectory>
  <executable>C:\Python310\python.exe</executable>
  <arguments>-u monitor.py --cli D:\path\to\easytier-cli.exe --log-file D:\path\to\easytier-monitor\logs\monitor.log --restart-cmd "D:\path\to\WinSW-x64.exe restart D:\path\to\easytier.xml"</arguments>
  <logpath>D:\path\to\easytier-monitor\logs</logpath>
  <log mode="roll-by-size"></log>
</service>
```

Key points:
- `--dependsOn` ensures the monitor starts after EasyTier
- `--cli` must be an absolute path since the service runs as LocalSystem with a limited PATH
- `--log-file` writes monitor logs directly to a file (recommended for service mode)
- `--restart-cmd` uses the same WinSW executable to restart the EasyTier service

## Linux (systemd)

```bash
python monitor.py --restart-cmd "systemctl restart easytier"
```

## Docker

```bash
python monitor.py --restart-cmd "docker restart easytier"
```
