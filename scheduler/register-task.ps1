#Requires -RunAsAdministrator
# Registers "JobSearch Daily": Mon-Fri 05:45, wake to run, run late if missed, 90-min limit.
# Second trigger: at logon (3-min delay). With -LogonType Interactive the 05:45 trigger is silently skipped
# when nobody is logged on (e.g. after an overnight update reboot); the logon trigger then runs the digest
# as soon as the owner signs in. The run exits at once when today's digest was already sent, and
# --weekdays-only keeps a weekend logon from sending one.
# RestartCount/RestartInterval only retry when the task fails to LAUNCH; a non-zero exit code from
# run.ps1 (2 send failed, 3 crashed, ...) is NOT retried. Rerun by hand: a plain rerun resends a saved digest.
param([ValidateSet("S4U", "Interactive")][string]$LogonType = "S4U")

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "C:\JobSearch\app\scheduler\run.ps1" --weekdays-only' `
  -WorkingDirectory "C:\JobSearch\app"
$daily = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At 5:45am
$logon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$logon.Delay = "PT3M"
$trigger = @($daily, $logon)
$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 90) -MultipleInstances IgnoreNew `
  -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 15)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType $LogonType -RunLevel Limited

Register-ScheduledTask -TaskName "JobSearch Daily" -Action $action -Trigger $trigger -Settings $settings `
  -Principal $principal -Description "Weekday job-alert digest (C:\JobSearch)" -Force | Out-Null
Get-ScheduledTask -TaskName "JobSearch Daily" | Select-Object TaskName, State, @{n="Logon";e={$_.Principal.LogonType}}
"Next run: " + (Get-ScheduledTaskInfo -TaskName 'JobSearch Daily').NextRunTime
