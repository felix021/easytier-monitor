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

## Linux (systemd)

```bash
python monitor.py --restart-cmd "systemctl restart easytier"
```

## Docker

```bash
python monitor.py --restart-cmd "docker restart easytier"
```
