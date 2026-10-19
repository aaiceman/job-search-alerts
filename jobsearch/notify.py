"""Best-effort Windows toast so a failed send is noticed. Never raises."""
from __future__ import annotations

import logging
import subprocess

log = logging.getLogger(__name__)
_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$x = $t.GetElementsByTagName('text')
$x.Item(0).AppendChild($t.CreateTextNode($env:TOAST_TITLE)) > $null
$x.Item(1).AppendChild($t.CreateTextNode($env:TOAST_BODY)) > $null
$id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show([Windows.UI.Notifications.ToastNotification]::new($t))
"""


def toast(title: str, body: str) -> None:
    import os
    try:
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _PS],
                       env={**os.environ, "TOAST_TITLE": title[:100], "TOAST_BODY": body[:250]},
                       timeout=30, capture_output=True)
    except Exception as e:
        log.warning("toast failed: %s", e)
