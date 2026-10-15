# One explicitly requested reply-generation check; never invoke fill, copy or send.
# Save verification flags only, without chat text, generated replies or credentials.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$manifestPath = Join-Path $root 'deployment.json'
$manifest = Get-Content -LiteralPath $manifestPath -Encoding UTF8 -Raw | ConvertFrom-Json
$exe = [System.IO.Path]::GetFullPath((Join-Path $root $manifest.entrypoint))
if (-not $exe.StartsWith($root.TrimEnd('\') + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Executable path is outside the deployment directory'
}
$appProcess = Get-Process -Name 'gpt-chat-helper' | Where-Object {
    $_.Path -eq $exe -and $_.MainWindowHandle -ne 0
} | Select-Object -First 1
if (-not $appProcess) { throw 'Deployed application is not running' }
$window = [System.Windows.Automation.AutomationElement]::FromHandle($appProcess.MainWindowHandle)
function Find-NamedControl([string]$name) {
    $condition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty, $name)
    $window.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $condition)
}
$generateName = -join [char[]]@(0x751f, 0x6210, 0x56de, 0x590d)
$refreshName = -join [char[]]@(0x5237, 0x65b0, 0x6a21, 0x578b)
$fillPrefix = -join [char[]]@(0x586b, 0x5165)
# Initial model discovery can invalidate outstanding jobs; wait until it has settled.
$deadline = (Get-Date).AddSeconds(25)
while ((Get-Date) -lt $deadline) {
    $refresh = Find-NamedControl $refreshName
    if (-not $refresh -or $refresh.Current.IsEnabled) { break }
    Start-Sleep -Milliseconds 300
}
$generate = Find-NamedControl $generateName
if (-not $generate -or -not $generate.Current.IsEnabled -or $generate.Current.IsOffscreen) {
    throw 'Reply-generation button is unavailable'
}
$invoke = $null
if (-not $generate.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$invoke)) {
    throw 'Reply-generation button has no InvokePattern'
}
$invoke.Invoke()
$deadline = (Get-Date).AddSeconds(100)
$ready = @()
while ((Get-Date) -lt $deadline) {
    $buttons = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            [System.Windows.Automation.ControlType]::Button)))
    $ready = @($buttons | Where-Object {
        $_.Current.Name.StartsWith($fillPrefix + ' ') -and $_.Current.IsEnabled
    })
    if ($ready.Count -gt 0) { break }
    Start-Sleep -Milliseconds 500
}
if ($ready.Count -eq 0) { throw 'No usable reply candidates appeared in the deployed application' }
$manifest | Add-Member -NotePropertyName 'live_packaged_reply_verified' -NotePropertyValue $true -Force
$manifest | Add-Member -NotePropertyName 'live_packaged_reply_candidate_count' -NotePropertyValue $ready.Count -Force
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
[pscustomobject]@{
    Version=$manifest.version
    ProcessId=$appProcess.Id
    PackagedReplyGenerationVerified=$true
    UsableCandidates=$ready.Count
    FillOrSendInvoked=$false
} | ConvertTo-Json
