# Entry point for the "JobSearch Daily" scheduled task.
# Windows PowerShell 5.1 turns native stderr (Python logging) into terminating errors under "Stop",
# so the preference is relaxed here; the exit code below is what matters.
$ErrorActionPreference = "Continue"
$App = "C:\JobSearch\app"
$Data = "C:\JobSearch\data"
$Log = "$Data\logs\task-stdout.log"
$env:JOBSEARCH_DATA = $Data
$env:PYTHONIOENCODING = "utf-8"
New-Item -ItemType Directory -Force -Path "$Data\logs" | Out-Null
Set-Location $App

# Fail loudly: a missing venv must not look like a successful run.
$py = "$App\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $py)) {
    "$(Get-Date -Format s) python missing: $py" | Out-File -FilePath $Log -Append -Encoding utf8
    exit 1
}

# Python writes UTF-8 (PYTHONIOENCODING); PowerShell decodes native output with the console code page
# (OEM 437 by default), which garbled em-dashes (mojibake) in task-stdout.log. Best-effort: a missing
# console handle must not stop the run.
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    "$(Get-Date -Format s) could not set UTF-8 console encoding: $($_.Exception.Message)" | Out-File -FilePath $Log -Append -Encoding utf8
}

# "$_" flattens stderr ErrorRecords to plain text; utf8 keeps the log readable (the default is UTF-16).
& $py -m jobsearch.run @args 2>&1 | ForEach-Object { "$_" } | Out-File -FilePath $Log -Append -Encoding utf8
$rc = $LASTEXITCODE   # python's exit code, captured before anything else can overwrite it
if ($null -eq $rc) { exit 1 }
exit $rc
