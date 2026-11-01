<#
.SYNOPSIS
    Browser access to the AutoFixOps Kubernetes target app: http://localhost:30081 -> service target-app-nodeport:80.

.DESCRIPTION
    Docker Desktop's kind-based cluster does not publish NodePorts on Windows, so this script keeps a
    kubectl port-forward on 127.0.0.1:30081 for the browser, through every chaos scenario (Memory Leak,
    CPU Spike, Crash Loop) and every remediation, without being restarted.

    * Kubernetes decides WHERE to forward. The script reads the service's selector and its
      port 80 -> targetPort mapping and forwards to the best pod behind it (running, not terminating,
      ready first, newest first). "kubectl port-forward svc/..." is not used: it resolves the service
      to one pod when it starts and stays on that pod after the pod is replaced.
    * HTTP decides whether the app is HEALTHY. /health and /ready are requested through the tunnel; a
      running kubectl process is never taken as health.
    * The tunnel is reopened when kubectl exits, when its pod is deleted or terminating, when a ready
      pod appears while the forwarded pod is not ready, and when HTTP checks get no answer although
      Kubernetes reports the pod ready (a tunnel that is alive but unusable).
    * kubectl closes a port-forward as soon as the app refuses one connection (crash, OOM restart,
      start-up). While Kubernetes reports the container as not running the script therefore does not
      probe, and it reopens the tunnel quietly with a growing delay instead of looping.

    The tunnel is only for viewing the app in a browser. AutoFixOps does not use it: Prometheus scrapes
    the node IP and recovery verification goes through the API server's service proxy (kube-service://).
    The script only reads from the cluster. Ctrl+C stops only the processes this script started.

.PARAMETER CheckOnly
    Run the prerequisite checks (kubectl, context, service, free port) and exit.
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)][int]$LocalPort = 30081,
    [string]$Context = 'docker-desktop',
    [string]$Namespace = 'autofixops',
    [string]$Service = 'target-app-nodeport',
    [ValidateRange(1, 65535)][int]$ServicePort = 80,
    [string]$ChaosConfigMap = 'target-app-chaos',   # read only, to tell a chaos outage from other outages
    [ValidateRange(1, 60)][int]$PollSeconds = 3,     # how often the pods behind the service are read
    [ValidateRange(1, 60)][int]$ProbeSeconds = 5,    # how often /health and /ready are requested
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$ConnectTimeoutSeconds = 15   # how long kubectl gets to report "Forwarding from ..."
$ProbeTimeoutMs = 4000
$UnusableAfter = 3            # unanswered checks in a row, while the pod is ready, before the tunnel is replaced
$MaxRetrySeconds = 10

# ---------------------------------------------------------------- output

$StateColors = @{
    'STARTING' = 'White'; 'WAITING FOR KUBERNETES' = 'Yellow'; 'WAITING FOR READY POD' = 'Yellow'
    'PORT-FORWARD CONNECTED' = 'Cyan'; 'APPLICATION HEALTHY' = 'Green'; 'APPLICATION NOT READY' = 'Yellow'
    'APPLICATION DOWN' = 'Red'; 'CHAOS OUTAGE DETECTED' = 'Magenta'; 'POD REPLACED' = 'Cyan'
    'RECONNECTING' = 'Cyan'; 'BROWSER ACCESS RESTORED' = 'Green'; 'STOPPED' = 'White'
    'OK' = 'Green'; 'INFO' = 'Gray'; 'WARN' = 'Yellow'; 'ERROR' = 'Red'
}

function Write-Status([string]$Label, [string]$Message) {
    $color = $StateColors[$Label]
    if (-not $color) { $color = 'Gray' }
    Write-Host ('[{0}] {1,-23} {2}' -f (Get-Date -Format 'HH:mm:ss'), $Label, $Message) -ForegroundColor $color
}

function Set-Title([string]$Text) {
    try { $Host.UI.RawUI.WindowTitle = "AutoFixOps target :$LocalPort - $Text" } catch { }
}

function Exit-WithError([string]$Message, [int]$Code = 1) {
    Write-Status ERROR $Message
    exit $Code
}

# ---------------------------------------------------------------- kubectl

function Find-Kubectl {
    $cmd = Get-Command kubectl -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    foreach ($candidate in @("$env:ProgramFiles\Docker\Docker\resources\bin\kubectl.exe",
                             "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin\kubectl.exe")) {
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

# Runs kubectl and captures stdout/stderr without PowerShell turning stderr into errors.
function Invoke-Kubectl([string[]]$Arguments, [int]$TimeoutSeconds = 20) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $script:Kubectl
    $psi.Arguments = ($Arguments | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join ' '
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    $p = [System.Diagnostics.Process]::Start($psi)
    $out = $p.StandardOutput.ReadToEndAsync()
    $err = $p.StandardError.ReadToEndAsync()
    if (-not $p.WaitForExit($TimeoutSeconds * 1000)) {
        try { $p.Kill() } catch { }
        return [pscustomobject]@{ Code = -1; Out = ''; Err = "kubectl did not answer within $TimeoutSeconds s" }
    }
    $p.WaitForExit()
    return [pscustomobject]@{ Code = $p.ExitCode; Out = $out.Result; Err = $err.Result.Trim() }
}

function Invoke-ClusterRead([string[]]$Arguments) {
    return Invoke-Kubectl (@('--context', $Context, '--namespace', $Namespace, '--request-timeout=10s') + $Arguments)
}

function Test-Unreachable([string]$ErrText) {
    return $ErrText -match 'Unable to connect|connection refused|actively refused|dial tcp|i/o timeout|did not answer|EOF|no such host'
}

function Get-FirstLine([string]$Text) {
    $line = "$($Text -split '\r?\n' | Where-Object { $_.Trim() } | Select-Object -First 1)"
    if ($line.Length -gt 160) { $line = $line.Substring(0, 160) + '...' }
    return $line
}

# ---------------------------------------------------------------- local port and processes

function Get-LauncherProcesses {
    @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe' or Name='pwsh.exe'" -ErrorAction SilentlyContinue |
      Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match 'start-target-access\.ps1' })
}

# Who, if anyone, is listening on the port. Any listener (also on ::1) is a conflict, because browsers
# try ::1 first for "localhost" and would reach that program instead of this tunnel.
function Get-PortConflict([int]$Port) {
    $listeners = @()
    # State is not always reported (it can read 0), so a wildcard remote address also marks a listener.
    # Sockets of a process that just exited can linger under PID 0 or a dead PID; they are not owners.
    try {
        $listeners = @(Get-NetTCPConnection -LocalPort $Port -ErrorAction Stop |
                       Where-Object { ($_.State -eq 'Listen' -or @('0.0.0.0', '::') -contains $_.RemoteAddress) -and
                                      $_.OwningProcess -gt 0 -and (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue) })
    } catch { }
    $bindError = $null
    $probe = $null
    try {
        $probe = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $Port)
        $probe.ExclusiveAddressUse = $true
        $probe.Start()
    } catch {
        $bindError = $_.Exception
        while ($bindError.InnerException) { $bindError = $bindError.InnerException }
    } finally {
        if ($probe) { try { $probe.Stop() } catch { } }
    }
    if ($listeners.Count -eq 0 -and -not $bindError) { return $null }

    $launchers = Get-LauncherProcesses
    $lines = @("Port $Port is already in use, so the tunnel cannot listen on it.")
    foreach ($procId in ($listeners | Select-Object -ExpandProperty OwningProcess -Unique)) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
        $addresses = ($listeners | Where-Object { $_.OwningProcess -eq $procId } | ForEach-Object { $_.LocalAddress }) -join ', '
        if (-not $proc) { $lines += "  PID $procId (process details unavailable) listening on $addresses"; continue }
        $lines += "  PID $procId  $($proc.Name)  listening on $addresses"
        if ($proc.CommandLine) { $lines += "    $($proc.CommandLine)" }
        # kubectl is the launcher's child, or its grandchild behind a kubectl shim.
        $owner = $null
        $ancestor = $proc
        for ($i = 0; $i -lt 3 -and $ancestor -and -not $owner; $i++) {
            $owner = $launchers | Where-Object { $_.ProcessId -eq $ancestor.ParentProcessId } | Select-Object -First 1
            if (-not $owner) { $ancestor = Get-CimInstance Win32_Process -Filter "ProcessId=$($ancestor.ParentProcessId)" -ErrorAction SilentlyContinue }
        }
        if ($owner) {
            $lines += "  Another START_TARGET_APP launcher (PowerShell PID $($owner.ProcessId)) already provides browser access."
            $lines += "  Use that window, or stop it with Ctrl+C there before starting a new one."
        } elseif ($proc.Name -eq 'kubectl.exe' -and $proc.CommandLine -match 'port-forward') {
            $lines += "  This is a kubectl port-forward that was started by hand. Note that 'port-forward svc/...' stays on"
            $lines += "  the pod it started with and stops working when chaos or remediation replaces that pod."
            $lines += "  Close its terminal, or run:  Stop-Process -Id $procId"
        } else {
            $lines += "  Stop that program, or start this script with another port:  -LocalPort <port>"
        }
    }
    if ($listeners.Count -eq 0 -and $bindError) {
        if ($bindError -is [System.Net.Sockets.SocketException] -and $bindError.ErrorCode -eq 10013) {
            $lines += "  Windows refused access to the port; it may be in a reserved range."
            $lines += "  Check with:  netsh interface ipv4 show excludedportrange protocol=tcp"
        } else {
            $lines += "  Binding 127.0.0.1:$Port failed: $($bindError.Message)"
        }
    }
    return ($lines -join [Environment]::NewLine)
}

# Children first (a kubectl shim from Chocolatey or Scoop runs the real kubectl as a child). Only
# processes created after the root are touched, so an unrelated process whose long-gone parent happened
# to have the same PID is never killed.
function Stop-ProcessTree([int]$Id, [datetime]$NotBefore) {
    foreach ($child in @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$Id" -ErrorAction SilentlyContinue)) {
        if ($child.CreationDate -and $child.CreationDate -ge $NotBefore) { Stop-ProcessTree ([int]$child.ProcessId) $NotBefore }
    }
    Stop-Process -Id $Id -Force -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------- Kubernetes view

function Get-TargetService {
    $r = Invoke-ClusterRead @('get', 'service', $Service, '-o', 'json')
    if ($r.Code -ne 0) { return [pscustomobject]@{ Error = $r.Err } }
    return [pscustomobject]@{ Error = $null; Json = ($r.Out | ConvertFrom-Json) }
}

function ConvertTo-PodInfo($pod) {
    $ready = @($pod.status.conditions) | Where-Object { $_ -and $_.type -eq 'Ready' -and $_.status -eq 'True' }
    $cs = @($pod.status.containerStatuses) | Where-Object { $_ } | Select-Object -First 1
    $container = 'unknown'; $reason = $null; $lastExit = $null; $restarts = 0
    if ($cs) {
        $restarts = [int]$cs.restartCount
        if ($cs.state.running) { $container = 'running' }
        elseif ($cs.state.waiting) { $container = 'waiting'; $reason = $cs.state.waiting.reason }
        elseif ($cs.state.terminated) { $container = 'terminated'; $reason = $cs.state.terminated.reason }
        if ($cs.lastState -and $cs.lastState.terminated) { $lastExit = $cs.lastState.terminated.reason }
    }
    $phase = $pod.status.phase
    if ($phase -eq 'Pending' -and $reason) { $phase = "Pending ($reason)" }
    return [pscustomobject]@{
        Name = $pod.metadata.name; Uid = $pod.metadata.uid; Phase = $phase
        Terminating = [bool]$pod.metadata.deletionTimestamp; Ready = [bool]$ready
        Container = $container; Reason = $reason; LastExit = $lastExit; Restarts = $restarts
        Created = [datetime]$pod.metadata.creationTimestamp; Spec = $pod.spec
    }
}

function Get-PodSnapshot {
    $r = Invoke-ClusterRead @('get', 'pods', '--selector', $script:Selector, '-o', 'json')
    if ($r.Code -ne 0) { return [pscustomobject]@{ Error = $r.Err; Pods = $null } }
    $pods = @(($r.Out | ConvertFrom-Json).items | Where-Object { $_ } | ForEach-Object { ConvertTo-PodInfo $_ })
    return [pscustomobject]@{ Error = $null; Pods = $pods }
}

# The pod to forward to: running and not terminating; ready first, then a running container, newest first.
function Select-BestPod($Pods) {
    return @($Pods) | Where-Object { $_ -and $_.Phase -eq 'Running' -and -not $_.Terminating } |
        Sort-Object @{ Expression = { $_.Ready }; Descending = $true },
                    @{ Expression = { $_.Container -eq 'running' }; Descending = $true },
                    @{ Expression = { $_.Created }; Descending = $true } |
        Select-Object -First 1
}

function Format-Pod($p) {
    $text = "pod $($p.Name): $($p.Phase), ready $($p.Ready.ToString().ToLower()), container $($p.Container)"
    if ($p.Reason) { $text += " ($($p.Reason))" }
    if ($p.Restarts) { $text += ", restarts $($p.Restarts)" }
    if ($p.LastExit) { $text += ", last exit $($p.LastExit)" }
    if ($p.Terminating) { $text += ', terminating' }
    return $text
}

# Service port -> the pod port it routes to (a number, or a named container port).
function Resolve-TargetPort($pod) {
    $tp = $script:TargetPort
    if ("$tp" -match '^\d+$') { return [int]$tp }
    foreach ($c in @($pod.Spec.containers)) {
        foreach ($p in @($c.ports)) { if ($p -and $p.name -eq $tp) { return [int]$p.containerPort } }
    }
    return $null
}

function ConvertTo-Number($Text) {
    $v = 0.0
    if ([double]::TryParse("$Text", [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$v)) { return $v }
    return 0.0
}

# The chaos ConfigMap's active fault, if any. Read only when a state changes, to label it; health is
# always decided by HTTP.
function Get-ChaosSummary {
    if (-not $ChaosConfigMap) { return $null }
    $r = Invoke-ClusterRead @('get', 'configmap', $ChaosConfigMap, '-o', 'json')
    if ($r.Code -ne 0) { return $null }
    $data = ($r.Out | ConvertFrom-Json).data
    if (-not $data) { return $null }
    $expires = ConvertTo-Number $data.CHAOS_EXPIRES_AT
    if ($expires -le [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) { return $null }
    $faults = @()
    if ((ConvertTo-Number $data.LEAK_MB_PER_MIN) -gt 0) { $faults += "Memory Leak ($($data.LEAK_MB_PER_MIN) MB/min)" }
    if ((ConvertTo-Number $data.CPU_DEMAND_MILLICORES) -gt 0) { $faults += "CPU Spike ($($data.CPU_DEMAND_MILLICORES)m CPU demand)" }
    if ((ConvertTo-Number $data.STARTUP_MEMORY_MB) -gt 0) { $faults += "Crash Loop ($($data.STARTUP_MEMORY_MB) MB at start-up)" }
    if ($faults.Count -eq 0) { return $null }
    $until = [DateTimeOffset]::FromUnixTimeSeconds([long]$expires).LocalDateTime.ToString('HH:mm')
    return ($faults -join ', ') + " configured until $until"
}

# ---------------------------------------------------------------- tunnel

# New lines appended to a log file since the last read (kubectl keeps the file open while writing).
function Read-NewLines([string]$Path, [hashtable]$Offsets) {
    if (-not $Path -or -not (Test-Path $Path)) { return @() }
    $fs = [System.IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite')
    try {
        $start = 0
        if ($Offsets.ContainsKey($Path)) { $start = $Offsets[$Path] }
        if ($fs.Length -le $start) { return @() }
        $null = $fs.Seek($start, 'Begin')
        $text = (New-Object System.IO.StreamReader($fs)).ReadToEnd()
        $complete = $text.LastIndexOf("`n")
        if ($complete -lt 0) { return @() }
        $Offsets[$Path] = $start + [System.Text.Encoding]::UTF8.GetByteCount($text.Substring(0, $complete + 1))
        return @($text.Substring(0, $complete).Split("`n") | ForEach-Object { $_.TrimEnd("`r") } | Where-Object { $_ })
    } finally {
        $fs.Dispose()
    }
}

# Why kubectl closed the tunnel, from its error output.
function Get-ExitReason([string[]]$ErrLines) {
    $text = $ErrLines -join "`n"
    if ($text -match 'connection refused') { return 'refused' }
    if ($text -match 'failed to find sandbox|not found|lost connection to pod|container not running|is not running') { return 'pod-gone' }
    if ($text -match 'unable to listen|address already in use|bind:') { return 'port' }
    return 'other'
}

function Start-Forward($pod, [int]$PodPort) {
    $script:Starts++
    $script:OutLog = Join-Path $script:LogDir "forward-$($script:Starts)-out.log"
    $script:ErrLog = Join-Path $script:LogDir "forward-$($script:Starts)-err.log"
    $script:ErrSeen = @()
    $script:ForwardStartedAt = Get-Date
    $script:Forward = Start-Process -FilePath $script:Kubectl -NoNewWindow -PassThru `
        -RedirectStandardOutput $script:OutLog -RedirectStandardError $script:ErrLog `
        -ArgumentList @('--context', $Context, '--namespace', $Namespace, 'port-forward',
                        '--address', '127.0.0.1', "pod/$($pod.Name)", "$($LocalPort):$PodPort")
    $null = $script:Forward.Handle   # keeps ExitCode available after exit (Windows PowerShell 5.1)
    $deadline = (Get-Date).AddSeconds($ConnectTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-CtrlC) { return 'stopped' }
        $exited = $script:Forward.HasExited
        if ((Read-NewLines $script:OutLog $script:Offsets) -match 'Forwarding from') {
            # Started. If a request already made it exit again (the app refused it), the main loop
            # handles that like any other exit.
            $script:ForwardPod = $pod
            return $null
        }
        if ($exited) { break }
        Start-Sleep -Milliseconds 200
    }
    $why = (Read-NewLines $script:ErrLog $script:Offsets) | Select-Object -Last 1
    if (-not $why) { $why = "no 'Forwarding from' within $ConnectTimeoutSeconds s" }
    Stop-Forward
    return $why
}

function Stop-Forward {
    if ($script:Forward) {
        if (-not $script:Forward.HasExited) {
            Stop-ProcessTree $script:Forward.Id $script:ForwardStartedAt.AddSeconds(-1)
            try { $null = $script:Forward.WaitForExit(5000) } catch { }
        }
        Remove-Item -Force -ErrorAction SilentlyContinue -Path $script:OutLog, $script:ErrLog
    }
    $script:Forward = $null
    $script:ForwardPod = $null
}

# One GET through the tunnel on a fresh connection. Kind: http (any status), timeout or transport.
function Invoke-Probe([string]$Path) {
    $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$LocalPort$Path")
    $req.Proxy = $null
    $req.KeepAlive = $false
    $req.Timeout = $ProbeTimeoutMs
    $req.ReadWriteTimeout = $ProbeTimeoutMs
    $resp = $null
    try {
        $resp = $req.GetResponse()
    } catch {
        $e = $_.Exception
        while ($e -and -not ($e -is [System.Net.WebException])) { $e = $e.InnerException }
        if ($e -and $e.Response) {
            $resp = $e.Response
        } else {
            $kind = 'transport'
            $detail = 'no answer'
            if ($e) {
                $detail = "$($e.Status)"
                if ($e.Status -eq [System.Net.WebExceptionStatus]::Timeout) { $kind = 'timeout' }
            }
            return [pscustomobject]@{ Kind = $kind; Code = 0; Status = $null; Detail = $detail }
        }
    }
    $code = [int]$resp.StatusCode
    $status = $null
    try {
        $body = (New-Object System.IO.StreamReader($resp.GetResponseStream())).ReadToEnd()
        $status = ($body | ConvertFrom-Json).status
    } catch { } finally { $resp.Close() }
    return [pscustomobject]@{ Kind = 'http'; Code = $code; Status = $status; Detail = $null }
}

function Format-Probe($p) {
    if ($p.Kind -ne 'http') { return $p.Detail }
    if ($p.Status) { return "HTTP $($p.Code) ($($p.Status))" }
    return "HTTP $($p.Code)"
}

# ---------------------------------------------------------------- console

# Sticky: once Ctrl+C was pressed, every later check says so.
function Test-CtrlC {
    if ($script:StopRequested) { return $true }
    if (-not $script:KeyInput) { return $false }
    while ([Console]::KeyAvailable) {
        $key = [Console]::ReadKey($true)
        if ($key.Key -eq 'C' -and ($key.Modifiers -band [ConsoleModifiers]::Control)) { $script:StopRequested = $true }
    }
    return $script:StopRequested
}

function Wait-Seconds([double]$Seconds) {
    $until = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $until) {
        if (Test-CtrlC) { return $false }
        Start-Sleep -Milliseconds 200
    }
    return $true
}

# In a classic console window, selecting text (QuickEdit) pauses every program that writes to it, so a
# click in the window would freeze the launcher mid-demo. Returns the mode to restore, or $null.
function Disable-QuickEdit {
    try {
        Add-Type -Namespace AutoFixOps -Name ConsoleMode -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern System.IntPtr GetStdHandle(int n);
[DllImport("kernel32.dll")] public static extern bool GetConsoleMode(System.IntPtr h, out uint m);
[DllImport("kernel32.dll")] public static extern bool SetConsoleMode(System.IntPtr h, uint m);
'@
        $handle = [AutoFixOps.ConsoleMode]::GetStdHandle(-10)
        [uint32]$mode = 0
        if (-not [AutoFixOps.ConsoleMode]::GetConsoleMode($handle, [ref]$mode)) { return $null }
        if (($mode -band 0x40) -eq 0) { return $null }
        $null = [AutoFixOps.ConsoleMode]::SetConsoleMode($handle, [uint32](($mode - 0x40) -bor 0x80))
        return $mode
    } catch {
        return $null
    }
}

function Restore-ConsoleMode($Mode) {
    if ($null -eq $Mode) { return }
    try { $null = [AutoFixOps.ConsoleMode]::SetConsoleMode([AutoFixOps.ConsoleMode]::GetStdHandle(-10), [uint32]$Mode) } catch { }
}

# ---------------------------------------------------------------- states

# Lifecycle and application states are printed only when they change.
function Set-State([string]$Label, [string]$Message, [string]$Key = '') {
    if ("$Label|$Key" -eq $script:ShownState) { return }
    $script:ShownState = "$Label|$Key"
    Write-Status $Label $Message
    Set-Title $Label
    if ($Label -in @('WAITING FOR KUBERNETES', 'WAITING FOR READY POD', 'POD REPLACED', 'RECONNECTING')) { $script:BrowserLost = $true }
}

function Set-AppState([string]$Label, [string]$Message) {
    if ($Label -eq 'APPLICATION HEALTHY') {
        if (-not $script:BrowserLost -and $script:ShownState -eq 'APPLICATION HEALTHY|') { return }
        $chaos = Get-ChaosSummary
        $note = ''
        if ($chaos) { $note = " Chaos is still configured ($chaos): the app answers now, but recovery is confirmed by AutoFixOps verification, not by this window." }
        if ($script:BrowserLost) {
            $script:BrowserLost = $false
            Set-State 'BROWSER ACCESS RESTORED' "$Message. Refresh the browser tab.$note" 'restored'
            $script:ShownState = 'APPLICATION HEALTHY|'
            Set-Title 'APPLICATION HEALTHY'
        } else {
            Set-State 'APPLICATION HEALTHY' "$Message.$note"
        }
        $script:OutageAnnounced = $false
        $script:QuietReopenNoted = $false
        return
    }
    $script:BrowserLost = $true
    if (-not $script:OutageAnnounced) {
        $script:OutageAnnounced = $true
        $script:QuietReopenNoted = $false
        $chaos = Get-ChaosSummary
        if ($chaos) {
            Set-State 'CHAOS OUTAGE DETECTED' "$chaos. Expected outage: AutoFixOps should detect it; this window keeps running and restores browser access after recovery." 'chaos'
        }
    }
    Set-State $Label $Message
}

# ---------------------------------------------------------------- prerequisite checks

Write-Host ''
Write-Host 'AutoFixOps target app - browser access' -ForegroundColor White
Write-Host "  localhost:$LocalPort  ->  service $Namespace/$Service port $ServicePort  (context $Context)" -ForegroundColor DarkGray
Write-Host ''

$script:Kubectl = Find-Kubectl
if (-not $script:Kubectl) {
    Exit-WithError 'kubectl was not found. Install Docker Desktop with Kubernetes enabled (it ships kubectl), or add kubectl to PATH.'
}
Write-Status OK "kubectl: $script:Kubectl"

$contexts = Invoke-Kubectl @('config', 'get-contexts', '-o', 'name')
if ($contexts.Code -ne 0) { Exit-WithError "kubectl could not read its configuration: $($contexts.Err)" }
if (($contexts.Out -split '\r?\n') -notcontains $Context) {
    Exit-WithError ("Kubernetes context '$Context' does not exist. Enable it in Docker Desktop > Settings > Kubernetes. " +
                    "Contexts found: " + ((($contexts.Out -split '\r?\n') | Where-Object { $_ }) -join ', '))
}
Write-Status OK "context '$Context' exists (used explicitly; your current kubectl context is not changed)"

$svc = Get-TargetService
if ($svc.Error) {
    if ($svc.Error -match 'NotFound|not found') {
        Exit-WithError "Service '$Service' was not found in namespace '$Namespace'. Deploy the demo first: python scripts/k8s_demo.py setup"
    }
    if (Test-Unreachable $svc.Error) {
        Exit-WithError "The Kubernetes cluster '$Context' is not reachable. Is Docker Desktop running with Kubernetes started? ($(Get-FirstLine $svc.Error))"
    }
    Exit-WithError "Could not read service '$Service': $($svc.Error)"
}
$portSpec = @($svc.Json.spec.ports) | Where-Object { $_.port -eq $ServicePort } | Select-Object -First 1
if (-not $portSpec) {
    Exit-WithError "Service '$Service' has no port $ServicePort (ports: $((@($svc.Json.spec.ports) | ForEach-Object { $_.port }) -join ', '))."
}
$selectorPairs = @()
if ($svc.Json.spec.selector) {
    $selectorPairs = @($svc.Json.spec.selector.PSObject.Properties | ForEach-Object { "$($_.Name)=$($_.Value)" })
}
if ($selectorPairs.Count -eq 0) { Exit-WithError "Service '$Service' has no pod selector, so there is no pod to forward to." }
$script:Selector = $selectorPairs -join ','
$script:TargetPort = $portSpec.targetPort
if ($null -eq $script:TargetPort) { $script:TargetPort = $ServicePort }
Write-Status OK "service '$Service' found: port $ServicePort -> pod port $($script:TargetPort), selector $script:Selector"

$conflict = Get-PortConflict $LocalPort
if ($conflict) { Exit-WithError $conflict 2 }
Write-Status OK "port $LocalPort is free on this machine"
foreach ($other in Get-LauncherProcesses) {
    Write-Status WARN "another launcher is running (PowerShell PID $($other.ProcessId)); only one can use port $LocalPort"
}

if ($CheckOnly) {
    Write-Status OK 'all checks passed (-CheckOnly: not starting the tunnel)'
    exit 0
}

# ---------------------------------------------------------------- monitoring loop

# Ctrl+C is read as a key so the tunnel is shut down by this script (and the .bat launcher does not
# ask "Terminate batch job?"). Without an interactive console, Ctrl+C stops the script and the
# finally block below still cleans up.
$script:KeyInput = $false
$script:StopRequested = $false
try { $null = [Console]::KeyAvailable; [Console]::TreatControlCAsInput = $true; $script:KeyInput = $true } catch { }
$consoleMode = $null
if ($script:KeyInput) { $consoleMode = Disable-QuickEdit }

$script:LogDir = Join-Path ([System.IO.Path]::GetTempPath()) "autofixops-target-access-$PID"
$null = New-Item -ItemType Directory -Force -Path $script:LogDir
$script:Offsets = @{}
$script:Forward = $null
$script:ForwardPod = $null
$script:Starts = 0
$script:ShownState = ''
$script:BrowserLost = $false
$script:OutageAnnounced = $false
$script:QuietReopenNoted = $false

$pods = $null            # last snapshot of the pods behind the service; $null while Kubernetes is unreachable
$clusterDown = $false
$retry = 0               # tunnel failures in a row while the pod is not ready (sets the reopen delay)
$failures = 0            # unanswered checks in a row while Kubernetes reports the pod ready
$reopenAt = Get-Date
$reopenWhenReady = $false
$nextPoll = Get-Date
$nextProbe = Get-Date
$lastRestarts = @{}
$lastRestartNote = [datetime]::MinValue
$lastConflictNote = [datetime]::MinValue
$lastErrorNote = [datetime]::MinValue

Write-Host '  Browser:' -ForegroundColor White
Write-Host "    http://localhost:$LocalPort/health" -ForegroundColor Green
Write-Host "    http://localhost:$LocalPort/ready" -ForegroundColor Green
Write-Host '  Keep this window open for the whole demo; Ctrl+C stops browser access.' -ForegroundColor DarkGray
Write-Host '  After "BROWSER ACCESS RESTORED", refresh the browser tab (the launcher cannot refresh it).' -ForegroundColor DarkGray
Write-Host ''
Set-State 'STARTING' "watching pods with selector $script:Selector every $PollSeconds s; checking /health and /ready every $ProbeSeconds s"

try {
    while (-not (Test-CtrlC)) {
      try {
        $now = Get-Date

        # 1. What Kubernetes reports about the pods behind the service.
        if ($now -ge $nextPoll) {
            $nextPoll = $now.AddSeconds($PollSeconds)
            $snap = Get-PodSnapshot
            if ($snap.Error) {
                $pods = $null
                if (-not $clusterDown) {
                    $clusterDown = $true
                    $msg = "cannot read the pods ($(Get-FirstLine $snap.Error)); retrying every $PollSeconds s"
                    if ($script:Forward) { Write-Status WARN "$msg. The current tunnel is kept." }
                    else { Set-State 'WAITING FOR KUBERNETES' $msg }
                }
            } else {
                if ($clusterDown) { $clusterDown = $false; Write-Status INFO 'Kubernetes is reachable again' }
                $pods = $snap.Pods
                foreach ($p in $pods) {
                    if ($lastRestarts.ContainsKey($p.Uid) -and $p.Restarts -gt $lastRestarts[$p.Uid] -and
                        ($now - $lastRestartNote).TotalSeconds -ge 30) {
                        $lastRestartNote = $now
                        Write-Status WARN "container restarted in place: $(Format-Pod $p)"
                    }
                    $lastRestarts[$p.Uid] = $p.Restarts
                }
            }
        }

        # 2. Is the tunnel still pointing at the right, live pod?
        if ($script:Forward) {
            $script:ErrSeen += @(Read-NewLines $script:ErrLog $script:Offsets)
            $null = Read-NewLines $script:OutLog $script:Offsets   # "Handling connection ..." lines
            $current = $null
            if ($null -ne $pods) { $current = @($pods) | Where-Object { $_.Uid -eq $script:ForwardPod.Uid } | Select-Object -First 1 }

            if ($script:Forward.HasExited) {
                $reason = Get-ExitReason $script:ErrSeen
                $podName = $script:ForwardPod.Name
                $podReady = [bool]($current -and $current.Ready)
                Stop-Forward
                if ($reason -eq 'refused' -and -not $podReady) {
                    # Expected while the app is down: every refused request ends the tunnel.
                    if (-not $script:QuietReopenNoted) {
                        $script:QuietReopenNoted = $true
                        Write-Status INFO "the app in pod $podName refused a connection, so kubectl closed the tunnel; it is reopened automatically while the outage lasts"
                    }
                } else {
                    $why = @{ refused = 'the app refused a connection'; 'pod-gone' = 'kubectl lost the pod';
                              port = 'the local port is unavailable'; other = 'kubectl exited' }[$reason]
                    Set-State 'RECONNECTING' "port-forward to pod $podName stopped ($why)" "exit-$($script:Starts)"
                }
                $delay = 1
                if (-not $podReady) {
                    $delay = [Math]::Min($MaxRetrySeconds, [Math]::Pow(2, [Math]::Min($retry, 4))); $retry++
                    $reopenWhenReady = $true   # the delay only waits for the app; skip it once the pod is ready
                }
                $reopenAt = (Get-Date).AddSeconds($delay)
            } elseif ($null -ne $pods) {   # (an empty list is a valid snapshot: every pod is gone)
                $best = Select-BestPod $pods
                if (-not $current -or $current.Terminating -or $current.Phase -ne 'Running') {
                    $what = 'was deleted'
                    if ($current -and $current.Terminating) { $what = 'is terminating' }
                    elseif ($current) { $what = "is $($current.Phase)" }
                    Set-State 'POD REPLACED' "pod $($script:ForwardPod.Name) $what; moving the tunnel to its replacement" "gone-$($script:ForwardPod.Uid)"
                    Stop-Forward
                    $reopenAt = Get-Date
                } elseif (-not $current.Ready -and $best -and $best.Ready -and $best.Uid -ne $current.Uid) {
                    Set-State 'POD REPLACED' "ready pod $($best.Name) is available while pod $($current.Name) is not ready; moving the tunnel" "switch-$($best.Uid)"
                    Stop-Forward
                    $reopenAt = Get-Date
                } else {
                    if ($current.Ready -and -not $script:ForwardPod.Ready) { $retry = 0 }
                    $script:ForwardPod = $current
                }
            }
        }

        # 3. Open the tunnel when there is none.
        if (-not $script:Forward -and $reopenWhenReady -and $null -ne $pods -and (Select-BestPod $pods).Ready) {
            $reopenAt = Get-Date
        }
        if (-not $script:Forward -and (Get-Date) -ge $reopenAt) {
            $reopenWhenReady = $false
            if ($null -eq $pods) {
                if ($clusterDown) { Set-State 'WAITING FOR KUBERNETES' 'the cluster is not reachable; the tunnel opens when it is' }
            } else {
                $best = Select-BestPod $pods
                if (-not $best) {
                    $what = 'no pods'
                    if (@($pods).Count) { $what = (@($pods) | ForEach-Object { Format-Pod $_ }) -join '; ' }
                    Set-State 'WAITING FOR READY POD' "no running pod behind service '$Service' yet ($what)" 'none'
                } else {
                    if ($best.Ready) { $retry = 0 }
                    $podPort = Resolve-TargetPort $best
                    $conflict = $null
                    if (-not $podPort) { $conflict = "pod $($best.Name) has no container port named '$($script:TargetPort)'" }
                    else { $conflict = Get-PortConflict $LocalPort }
                    if ($conflict) {
                        if (((Get-Date) - $lastConflictNote).TotalSeconds -ge 30) { $lastConflictNote = Get-Date; Write-Status ERROR $conflict }
                        $reopenAt = (Get-Date).AddSeconds([Math]::Min($MaxRetrySeconds, [Math]::Pow(2, [Math]::Min($retry, 4))))
                        $retry++
                    } else {
                        $failed = Start-Forward $best $podPort
                        if ($failed -eq 'stopped') { break }
                        if ($failed) {
                            Set-State 'RECONNECTING' "port-forward to pod $($best.Name) did not start: $failed; retrying" "start-$($script:Starts)"
                            $delay = [Math]::Min($MaxRetrySeconds, [Math]::Pow(2, [Math]::Min($retry, 4))); $retry++
                            $reopenAt = (Get-Date).AddSeconds($delay)
                        } else {
                            $failures = 0
                            $nextProbe = Get-Date
                            if (-not $script:QuietReopenNoted -or $best.Ready) {
                                Set-State 'PORT-FORWARD CONNECTED' "127.0.0.1:$LocalPort -> $(Format-Pod $best), port $podPort" "pf-$($best.Uid)"
                            }
                        }
                    }
                }
            }
        }

        # 4. What the application answers through the tunnel.
        if ($script:Forward -and (Get-Date) -ge $nextProbe) {
            $nextProbe = (Get-Date).AddSeconds($ProbeSeconds)
            $pod = $script:ForwardPod
            if ($pod.Container -ne 'running') {
                # Nothing listens in the pod; a request would only make kubectl close the tunnel.
                Set-AppState 'APPLICATION DOWN' "the app is not running: $(Format-Pod $pod)"
            } else {
                $health = Invoke-Probe '/health'
                $ready = $null
                if ($health.Kind -eq 'http') { $ready = Invoke-Probe '/ready' }
                if ($health.Kind -eq 'http' -and $health.Code -eq 200 -and $ready.Kind -eq 'http' -and $ready.Code -eq 200) {
                    $failures = 0; $retry = 0
                    Set-AppState 'APPLICATION HEALTHY' "/health HTTP 200, /ready HTTP 200 from pod $($pod.Name)"
                } elseif ($health.Kind -eq 'http') {
                    # The tunnel works (the app answered); the app itself is unwell.
                    $failures = 0; $retry = 0
                    $label = 'APPLICATION NOT READY'
                    if ($health.Code -ne 200) { $label = 'APPLICATION DOWN' }
                    Set-AppState $label "/health $(Format-Probe $health), /ready $(Format-Probe $ready) from pod $($pod.Name) (ready in Kubernetes: $($pod.Ready.ToString().ToLower()))"
                } elseif ($pod.Ready) {
                    $failures++
                    if ($failures -ge $UnusableAfter) {
                        Set-State 'RECONNECTING' "port-forward unusable: $failures checks in a row got no answer ($($health.Detail)) although Kubernetes reports pod $($pod.Name) ready" "unusable-$($script:Starts)"
                        Stop-Forward
                        $failures = 0
                        $reopenAt = (Get-Date).AddSeconds(1)
                    }
                } else {
                    Set-AppState 'APPLICATION DOWN' "no answer from the app ($($health.Detail)): $(Format-Pod $pod)"
                }
            }
        }
      } catch {
        # Never let one bad answer end browser access in the middle of a demo.
        if (((Get-Date) - $lastErrorNote).TotalSeconds -ge 30) {
            $lastErrorNote = Get-Date
            Write-Status WARN "unexpected error, continuing: $($_.Exception.Message)"
        }
      }

        if (-not (Wait-Seconds 1)) { break }
    }
} finally {
    Stop-Forward
    Restore-ConsoleMode $consoleMode
    if ($script:KeyInput) { try { [Console]::TreatControlCAsInput = $false } catch { } }
    Remove-Item -Recurse -Force -Path $script:LogDir -ErrorAction SilentlyContinue
    Write-Host ''
    Write-Status 'STOPPED' "browser access stopped; port $LocalPort released. Nothing in the cluster was changed."
    Set-Title 'STOPPED'
}
exit 0
