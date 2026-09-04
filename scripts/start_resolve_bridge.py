#!/usr/bin/env python3
"""Start the in-app bridge without touching Resolve's menus by hand.

    python scripts/start_resolve_bridge.py

`install_resolve_bridge.py` puts `resolve_bridge` in **Workspace ▸ Scripts**.
Someone still has to click it, once per Resolve session, before the server can
reach a free-edition Resolve at all — and the click cannot be replaced by
running the script from a shell, because the bridge only works when Resolve
itself launches it. A Scripts-menu script is handed the live `resolve` object;
the same file run from a terminal is a foreign process and gets the refusal the
bridge exists to route around.

So the click is automated instead of removed. On Windows the menu bar is a real
UI Automation tree — every item exposes ExpandCollapse and Invoke — so this
walks Workspace ▸ Scripts ▸ resolve_bridge and invokes it, then waits for the
listener to answer before claiming anything.

**Windows only.** The automation is `System.Windows.Automation` driven through
PowerShell. macOS would need System Events and its accessibility consent, Linux
depends on the toolkit; neither is implemented, and both are told to click the
menu themselves rather than left to guess why nothing happened.

## What it will not do

It will not launch Resolve, and it will not open a project. Both are the user's
call — and the second is not optional for reasons that are easy to misread: the
Scripts menu is *empty* while Resolve sits in the Project Manager, so a run
against a projectless Resolve fails with "no resolve_bridge item" that looks
exactly like a missing install. That case is detected and named.

Idempotent: an already-answering bridge is reported and left alone. Two bridges
cannot serve one port anyway, and the second would fail after the menu had
already been driven for no reason.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.utils import resolve_runtime as runtime  # noqa: E402
from src.utils.resolve_bridge_client import config_path  # noqa: E402

#: Shells tried in order, matching `resolve_runtime`. Windows PowerShell carries
#: the UI Automation assemblies on every supported Windows; `pwsh` is tried
#: second because a machine may have only it.
SHELLS = ("powershell.exe", "pwsh.exe")

#: How long to wait for the listener after invoking the menu item. The bridge
#: starts a Python interpreter inside Resolve and imports its runtime, which is
#: seconds on a cold filesystem cache.
LISTENER_TIMEOUT_SECONDS = 30.0

#: The PowerShell side. Takes the Resolve PID, walks the menu, invokes the item.
#:
#: Two details are load-bearing and were both found the hard way:
#:
#: - The submenu is looked up from the window when it is not found under the
#:   parent item. Qt builds each popup as its own top-level element, so a
#:   freshly expanded submenu is not always a descendant of the item that
#:   opened it.
#: - Menus are closed with Escape first. A menu left open by an earlier run
#:   makes the *next* lookup of the same item return nothing, which reads as
#:   "Resolve has no Workspace menu" and is nothing of the sort.
#:
#: The item name is passed in rather than inlined so the failure message can
#: distinguish the two levels it walks.
_AUTOMATION_PS = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class NativeWin {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
}
"@

$AE = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]
$CT = [System.Windows.Automation.ControlType]

function Find-MenuItem($parent, $name) {
    $cond = New-Object System.Windows.Automation.AndCondition(
        (New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, $CT::MenuItem)),
        (New-Object System.Windows.Automation.PropertyCondition($AE::NameProperty, $name)))
    return $parent.FindFirst($TS::Descendants, $cond)
}

function Expand-MenuItem($item) {
    $pattern = $item.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern)
    $pattern.Expand()
    Start-Sleep -Milliseconds 400
}

$pidCond = New-Object System.Windows.Automation.PropertyCondition($AE::ProcessIdProperty, [int]$env:BRIDGE_RESOLVE_PID)
$window = $AE::RootElement.FindFirst($TS::Children, $pidCond)
if (-not $window) { Write-Output 'ERROR|no-window'; exit 1 }

[void][NativeWin]::SetForegroundWindow([IntPtr]$window.Current.NativeWindowHandle)
Start-Sleep -Milliseconds 400
# A menu left open by an earlier run hides the items from this one.
[System.Windows.Forms.SendKeys]::SendWait('{ESC}')
Start-Sleep -Milliseconds 200
[System.Windows.Forms.SendKeys]::SendWait('{ESC}')
Start-Sleep -Milliseconds 300

$workspace = Find-MenuItem $window 'Workspace'
if (-not $workspace) { Write-Output 'ERROR|no-workspace-menu'; exit 1 }
Expand-MenuItem $workspace

$scripts = Find-MenuItem $workspace 'Scripts'
if (-not $scripts) { $scripts = Find-MenuItem $window 'Scripts' }
if (-not $scripts) { Write-Output 'ERROR|no-scripts-menu'; exit 1 }
Expand-MenuItem $scripts

$target = Find-MenuItem $scripts $env:BRIDGE_MENU_ITEM
if (-not $target) { $target = Find-MenuItem $window $env:BRIDGE_MENU_ITEM }
if (-not $target) {
    [System.Windows.Forms.SendKeys]::SendWait('{ESC}')
    Write-Output 'ERROR|no-bridge-item'
    exit 1
}

$invoke = $target.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
$invoke.Invoke()
Write-Output 'OK|invoked'
"""

#: Failure tokens from the script above, and what each one actually means to
#: someone who has to fix it. `no-bridge-item` is the interesting one: the two
#: causes are unrelated and the wrong guess sends you to reinstall something
#: that is already installed.
_FAILURES = {
    "no-window": (
        "No DaVinci Resolve window is visible to UI Automation. If Resolve is "
        "running headless (-nogui) there is no menu to drive, and the free "
        "edition cannot be reached that way at all."
    ),
    "no-workspace-menu": (
        "Resolve's menu bar did not expose a Workspace menu. If a modal dialog "
        "is open, dismiss it and try again."
    ),
    "no-scripts-menu": (
        "Workspace has no Scripts submenu. Run "
        "`python scripts/install_resolve_bridge.py` and restart Resolve."
    ),
    "no-bridge-item": (
        "The Scripts menu has no resolve_bridge entry. Either no project is "
        "open — the menu is empty in the Project Manager, open a project first "
        "— or the bridge is not installed: run "
        "`python scripts/install_resolve_bridge.py` and restart Resolve."
    ),
}


def load_endpoint() -> Tuple[str, int]:
    """Host and port the bridge is configured to serve on.

    Read from the same config the client reads, rather than defaulted, so this
    script cannot start looking at a port nothing will ever answer on.
    """
    path = config_path()
    if not path.is_file():
        raise SystemExit(
            f"No bridge config at {path}.\n"
            "Run `python scripts/install_resolve_bridge.py` first — it writes "
            "the config and installs the menu script together."
        )
    config = json.loads(path.read_text(encoding="utf-8"))
    return str(config.get("host", "127.0.0.1")), int(config["port"])


def listener_is_up(host: str, port: int, timeout: float = 0.75) -> bool:
    """Is something accepting connections on the bridge port?

    A TCP connect, not a bridge request: this only needs to know whether the
    listener exists, and an unauthenticated probe is the cheapest way to ask
    without a token round-trip.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_listener(host: str, port: int, deadline_seconds: float) -> bool:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if listener_is_up(host, port):
            return True
        time.sleep(0.5)
    return False


def resolve_pids() -> Optional[List[int]]:
    """PIDs of running Resolve applications, or None when undeterminable.

    `resolve_runtime` reports command lines, not PIDs, so the PID is asked for
    separately — but only after that module has confirmed a Resolve is running,
    so a machine where process listing does not work at all still gets the
    honest "cannot tell" rather than an empty list read as "nothing running".
    """
    if runtime.resolve_processes() is None:
        return None
    query = (
        "$ErrorActionPreference='Stop';"
        "try { Get-CimInstance Win32_Process -Filter \"name='Resolve.exe'\" |"
        " ForEach-Object { $_.ProcessId } } catch { exit 1 }"
    )
    for shell in SHELLS:
        try:
            out = subprocess.run(
                [shell, "-NoProfile", "-NonInteractive", "-Command", query],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=20, check=False,
            )
        except Exception:
            continue
        if out.returncode == 0:
            return [int(line.strip()) for line in (out.stdout or "").splitlines() if line.strip().isdigit()]
    return None


def drive_menu(pid: int, item: str) -> Dict[str, Any]:
    """Invoke the menu item in the Resolve owning `pid`."""
    env = dict(os.environ, BRIDGE_RESOLVE_PID=str(pid), BRIDGE_MENU_ITEM=item)
    last_error = "no PowerShell available to drive the menu"
    for shell in SHELLS:
        try:
            out = subprocess.run(
                [shell, "-NoProfile", "-NonInteractive", "-Command", _AUTOMATION_PS],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=90, check=False, env=env,
            )
        except FileNotFoundError:
            continue
        except Exception as exc:  # pragma: no cover - defensive
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        text = (out.stdout or "") + (out.stderr or "")
        for token, explanation in _FAILURES.items():
            if f"ERROR|{token}" in text:
                return {"ok": False, "reason": token, "message": explanation}
        if "OK|invoked" in text:
            return {"ok": True}
        last_error = text.strip() or f"exit {out.returncode} with no output"
    return {"ok": False, "reason": "automation-failed", "message": last_error}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--item", default="resolve_bridge",
        help="Scripts-menu entry to invoke (default: resolve_bridge).",
    )
    parser.add_argument(
        "--timeout", type=float, default=LISTENER_TIMEOUT_SECONDS,
        help="Seconds to wait for the listener after invoking.",
    )
    parser.add_argument(
        "--pid", type=int, default=None,
        help="Drive this Resolve process instead of the one discovered.",
    )
    args = parser.parse_args()

    if sys.platform != "win32":
        print(
            "This script automates Windows UI Automation and does nothing "
            "elsewhere.\nOn macOS and Linux, start the bridge by hand: "
            "Workspace > Scripts > resolve_bridge.",
            file=sys.stderr,
        )
        return 2

    host, port = load_endpoint()
    if listener_is_up(host, port):
        print(f"Bridge already listening on {host}:{port} — nothing to do.")
        return 0

    if args.pid is not None:
        pid = args.pid
    else:
        pids = resolve_pids()
        if pids is None:
            print(
                "Cannot tell whether Resolve is running — the process list "
                "could not be read. Pass --pid to drive a known process.",
                file=sys.stderr,
            )
            return 1
        if not pids:
            print("DaVinci Resolve is not running. Start it and open a project first.", file=sys.stderr)
            return 1
        if len(pids) > 1:
            print(
                f"{len(pids)} Resolve processes are running ({', '.join(map(str, pids))}). "
                "Pass --pid to say which one should serve the bridge.",
                file=sys.stderr,
            )
            return 1
        pid = pids[0]

    print(f"Driving Workspace > Scripts > {args.item} in Resolve (pid {pid})...")
    outcome = drive_menu(pid, args.item)
    if not outcome["ok"]:
        print(f"Could not start the bridge: {outcome['message']}", file=sys.stderr)
        return 1

    # The menu item is invoked, which is not the same as the listener being up.
    # Reporting success here would be the exact claim this project treats as a
    # failure mode: an action taken, described as an outcome achieved.
    if not wait_for_listener(host, port, args.timeout):
        print(
            f"Invoked {args.item}, but nothing is listening on {host}:{port} "
            f"after {args.timeout:.0f}s.\nCheck Resolve's Console "
            "(Workspace > Console) for the script's own error.",
            file=sys.stderr,
        )
        return 1

    print(f"Bridge listening on {host}:{port}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
