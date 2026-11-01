<#
.SYNOPSIS
    Browser access to the AutoFixOps Kubernetes target app: http://localhost:30081 -> service target-app-nodeport:80.

.DESCRIPTION
    Docker Desktop's kind-based cluster does not publish NodePorts on Windows, so this script keeps a
    kubectl port-forward open on 127.0.0.1:30081 for the browser. It follows the service: it reads the
    service's selector and port 80 -> targetPort mapping, forwards to a running pod behind it, and
    reconnects when that pod is replaced (chaos, rollout, remediation) or when kubectl drops the tunnel.

    The tunnel is only for viewing the app in a browser. AutoFixOps does not use it: Prometheus scrapes
    the node IP and recovery verification goes through the API server's service proxy (kube-service://).
    The script only reads from the cluster; it never changes the deployment, service, ConfigMap or pods.

    Stop with Ctrl+C.

.PARAMETER CheckOnly
    Run the prerequisite checks (kubectl, context, service, running pod, free port) and exit.
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)][int]$LocalPort = 30081,
    [string]$Context = 'docker-desktop',
    [string]$Namespace = 'autofixops',
    [string]$Service = 'target-app-nodeport',
    [ValidateRange(1, 65535)][int]$ServicePort = 80,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$PodCheckSeconds = 3          # how often the forwarded pod is checked for replacement
$ConnectTimeoutSeconds = 15   # how long kubectl gets to report "Forwarding from ..."
$MaxBackoffSeconds = 15

function Write-Status([string]$Level, [string]$Message) {
    $colors = @{ INFO = 'Gray'; OK = 'Green'; WARN = 'Yellow'; ERROR = 'Red'; RETRY = 'Cyan' }
    Write-Host ('[{0}] {1,-5} {2}' -f (Get-Date -Format 'HH:mm:ss'), $Level, $Message) -ForegroundColor $colors[$Level]
}

function Exit-WithError([string]$Message, [int]$Code = 1) {
    Write-Status ERROR $Message
    exit $Code
}

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

# Who, if anyone, is listening on the port. Any listener (also on ::1) is a conflict, because browsers
# try ::1 first for "localhost" and would reach that program instead of this tunnel.
function Get-PortConflict([int]$Port) {
    $listeners = @()
    # State is not always reported (it can read 0), so a wildcard remote address also marks a listener.
    try {
        $listeners = @(Get-NetTCPConnection -LocalPort $Port -ErrorAction Stop |
                       Where-Object { $_.State -eq 'Listen' -or @('0.0.0.0', '::') -contains $_.RemoteAddress })
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

    $lines = @("Port $Port is already in use, so the tunnel cannot listen on it.")
    foreach ($procId in ($listeners | Select-Object -ExpandProperty OwningProcess -Unique)) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
        $addresses = ($listeners | Where-Object { $_.OwningProcess -eq $procId } | ForEach-Object { $_.LocalAddress }) -join ', '
        if ($proc) {
            $lines += "  PID $procId  $($proc.Name)  listening on $addresses"
            if ($proc.CommandLine) { $lines += "    $($proc.CommandLine)" }
            if ($proc.Name -eq 'kubectl.exe' -and $proc.CommandLine -match 'port-forward') {
                $lines += "  This is another kubectl port-forward (an earlier START_TARGET_APP window or a manual tunnel)."
                $lines += "  If you do not need it, close its window or run:  Stop-Process -Id $procId"
            } else {
                $lines += "  Stop that program, or start this script with another port:  -LocalPort <port>"
            }
        } else {
            $lines += "  PID $procId (process details unavailable) listening on $addresses"
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

function Get-TargetService {
    $r = Invoke-ClusterRead @('get', 'service', $Service, '-o', 'json')
    if ($r.Code -ne 0) { return [pscustomobject]@{ Error = $r.Err } }
    return [pscustomobject]@{ Error = $null; Json = ($r.Out | ConvertFrom-Json) }
}

function Get-PodReady($pod) {
    $cond = @($pod.status.conditions) | Where-Object { $_.type -eq 'Ready' } | Select-Object -First 1
    return ($cond -and $cond.status -eq 'True')
}

function Get-PodRestarts($pod) {
    $n = 0
    foreach ($cs in @($pod.status.containerStatuses)) { if ($cs) { $n += [int]$cs.restartCount } }
    return $n
}

# The pod to forward to: running and not terminating, ready ones first, newest first.
function Select-TargetPod {
    $r = Invoke-ClusterRead @('get', 'pods', '--selector', $script:Selector, '-o', 'json')
    if ($r.Code -ne 0) { return [pscustomobject]@{ Error = $r.Err; Pod = $null } }
    $candidates = @(($r.Out | ConvertFrom-Json).items | Where-Object {
        $_.status.phase -eq 'Running' -and -not $_.metadata.deletionTimestamp
    })
    $pod = $candidates | Sort-Object @{ Expression = { Get-PodReady $_ }; Descending = $true },
                                     @{ Expression = { [datetime]$_.metadata.creationTimestamp }; Descending = $true } |
           Select-Object -First 1
    return [pscustomobject]@{ Error = $null; Pod = $pod }
}

# Service port 80 -> the pod port it routes to (a number, or a named container port).
function Resolve-TargetPort($pod) {
    $tp = $script:TargetPort
    if ("$tp" -match '^\d+$') { return [int]$tp }
    foreach ($c in @($pod.spec.containers)) {
        foreach ($p in @($c.ports)) { if ($p -and $p.name -eq $tp) { return [int]$p.containerPort } }
    }
    return $null
}

# New lines appended to a log file since the last read (kubectl keeps the file open while writing).
function Read-NewLines([string]$Path, [hashtable]$Offsets) {
    if (-not (Test-Path $Path)) { return @() }
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

# Children first: a kubectl shim (Chocolatey, Scoop) runs the real kubectl as a child, which would
# otherwise keep holding the port.
function Stop-ProcessTree([int]$Id) {
    foreach ($child in @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$Id" -ErrorAction SilentlyContinue)) {
        Stop-ProcessTree ([int]$child.ProcessId)
    }
    Stop-Process -Id $Id -Force -ErrorAction SilentlyContinue
}

function Stop-Forward {
    if ($script:Forward -and -not $script:Forward.HasExited) {
        Stop-ProcessTree $script:Forward.Id
        try { $null = $script:Forward.WaitForExit(5000) } catch { }
    }
    $script:Forward = $null
}

# One GET through the tunnel. It only shows what the app answers right now; it says nothing about
# whether AutoFixOps considers the incident recovered.
function Get-HttpStatus([string]$Path) {
    try {
        $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$LocalPort$Path")
        $req.Proxy = $null
        $req.Timeout = 5000
        $resp = $req.GetResponse()
        $code = [int]$resp.StatusCode
        $resp.Close()
        return "HTTP $code"
    } catch [System.Net.WebException] {
        if ($_.Exception.Response) { return "HTTP $([int]$_.Exception.Response.StatusCode)" }
        return "no response ($($_.Exception.Status))"
    } catch {
        return "no response"
    }
}

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
        Start-Sleep -Milliseconds 250
    }
    return $true
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
        Exit-WithError "The Kubernetes cluster '$Context' is not reachable. Is Docker Desktop running with Kubernetes started? ($($svc.Error))"
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

$sel = Select-TargetPod
if ($sel.Error) { Exit-WithError "Could not list the pods behind the service: $($sel.Error)" }
if ($sel.Pod) {
    Write-Status OK "running pod behind the service: $($sel.Pod.metadata.name) (ready: $(Get-PodReady $sel.Pod))"
} else {
    Write-Status WARN 'no running pod behind the service right now; the tunnel will connect when one starts'
}

$conflict = Get-PortConflict $LocalPort
if ($conflict) { Exit-WithError $conflict 2 }
Write-Status OK "port $LocalPort is free on this machine"

if ($CheckOnly) {
    Write-Status OK 'all checks passed (-CheckOnly: not starting the tunnel)'
    exit 0
}

# ---------------------------------------------------------------- tunnel loop

# Ctrl+C is read as a key so the tunnel is shut down by this script (and the .bat launcher does not
# ask "Terminate batch job?"). Without an interactive console, Ctrl+C stops the script and the
# finally block below still cleans up.
$script:KeyInput = $false
$script:StopRequested = $false
try { $null = [Console]::KeyAvailable; [Console]::TreatControlCAsInput = $true; $script:KeyInput = $true } catch { }

$logDir = Join-Path ([System.IO.Path]::GetTempPath()) "autofixops-target-access-$PID"
$null = New-Item -ItemType Directory -Force -Path $logDir
$script:Forward = $null
$offsets = @{}
$attempt = 0          # failed attempts in a row (sets the backoff)
$started = 0          # kubectl starts so far; each gets its own log files
$everConnected = $false

try {
    while ($true) {
        if (Test-CtrlC) { break }

        # (Re)connect.
        if (-not $script:Forward) {
            $backoff = [Math]::Min($MaxBackoffSeconds, [Math]::Pow(2, [Math]::Min($attempt, 4)))
            $sel = Select-TargetPod
            $pod = $sel.Pod
            $problem = $null
            if ($sel.Error) {
                $problem = "cannot reach the cluster to find the target pod: $($sel.Error)"
            } elseif (-not $pod) {
                $problem = "no running pod behind service '$Service' yet (it is being replaced or is starting)"
            } else {
                $podPort = Resolve-TargetPort $pod
                if (-not $podPort) { $problem = "pod $($pod.metadata.name) has no container port named '$($script:TargetPort)'" }
            }
            if (-not $problem) {
                $c = Get-PortConflict $LocalPort
                if ($c) { $problem = $c }
            }
            if ($problem) {
                $attempt++
                Write-Status RETRY "$problem; retrying in $backoff s"
                if (-not (Wait-Seconds $backoff)) { break }
                continue
            }

            $podName = $pod.metadata.name
            $started++
            $outLog = Join-Path $logDir "forward-$started-out.log"
            $errLog = Join-Path $logDir "forward-$started-err.log"
            $script:Forward = Start-Process -FilePath $script:Kubectl -NoNewWindow -PassThru `
                -RedirectStandardOutput $outLog -RedirectStandardError $errLog `
                -ArgumentList @('--context', $Context, '--namespace', $Namespace, 'port-forward',
                                '--address', '127.0.0.1', "pod/$podName", "$($LocalPort):$podPort")
            $null = $script:Forward.Handle   # keeps ExitCode available after exit (Windows PowerShell 5.1)

            $connected = $false
            $deadline = (Get-Date).AddSeconds($ConnectTimeoutSeconds)
            while ((Get-Date) -lt $deadline -and -not $script:Forward.HasExited) {
                if (Test-CtrlC) { break }
                if ((Read-NewLines $outLog $offsets) -match 'Forwarding from') { $connected = $true; break }
                Start-Sleep -Milliseconds 250
            }
            if (-not $connected) {
                $why = (Read-NewLines $errLog $offsets) | Select-Object -Last 1
                if (-not $why) { $why = "no 'Forwarding from' within $ConnectTimeoutSeconds s" }
                Stop-Forward
                if (Test-CtrlC) { break }
                $attempt++
                Write-Status RETRY "port-forward to pod $podName did not start: $why; retrying in $backoff s"
                if (-not (Wait-Seconds $backoff)) { break }
                continue
            }

            $attempt = 0
            $watchedUid = $pod.metadata.uid
            $lastReady = Get-PodReady $pod
            $lastRestarts = Get-PodRestarts $pod
            $lastRefusedNote = [datetime]::MinValue
            $clusterWarned = $false
            $nextPodCheck = (Get-Date).AddSeconds($PodCheckSeconds)
            if (-not $everConnected) {
                $everConnected = $true
                Write-Status OK "tunnel up: 127.0.0.1:$LocalPort -> pod $podName port $podPort"
                Write-Host ''
                Write-Host '  Open in your browser:' -ForegroundColor White
                Write-Host "    http://localhost:$LocalPort/health" -ForegroundColor Green
                Write-Host "    http://localhost:$LocalPort/ready" -ForegroundColor Green
                Write-Host ''
                Write-Host '  Keep this window open. Press Ctrl+C to stop browser access.' -ForegroundColor DarkGray
                Write-Host '  The tunnel reconnects by itself when the pod is replaced.' -ForegroundColor DarkGray
                Write-Host ''
            } else {
                Write-Status OK "reconnected: 127.0.0.1:$LocalPort -> pod $podName port $podPort (refresh the browser)"
            }
            Write-Status INFO ("the app answers right now: /health $(Get-HttpStatus '/health'), /ready $(Get-HttpStatus '/ready') " +
                               '(what the browser will see; AutoFixOps verifies recovery separately)')
            continue
        }

        # Tunnel running: relay kubectl's messages, watch for exit and pod replacement.
        $null = Read-NewLines $outLog $offsets   # "Handling connection for ..." lines, one per request
        foreach ($line in (Read-NewLines $errLog $offsets)) {
            if ($line -match 'connection refused|connect: connection reset') {
                if (((Get-Date) - $lastRefusedNote).TotalSeconds -ge 30) {
                    $lastRefusedNote = Get-Date
                    Write-Status WARN "pod $podName refused the connection: the app is not listening (crashed or restarting). The tunnel is fine."
                }
            } elseif ($line -notmatch 'Handling connection') {
                Write-Status WARN "kubectl: $line"
            }
        }

        if ($script:Forward.HasExited) {
            Write-Status RETRY "port-forward to pod $podName stopped (exit code $($script:Forward.ExitCode)); reconnecting"
            $script:Forward = $null
            continue
        }

        if ((Get-Date) -ge $nextPodCheck) {
            $nextPodCheck = (Get-Date).AddSeconds($PodCheckSeconds)
            $r = Invoke-ClusterRead @('get', 'pod', $podName, '-o', 'json')
            if ($r.Code -ne 0 -and $r.Err -match 'NotFound|not found') {
                Write-Status RETRY "pod $podName was deleted (replaced); reconnecting to its replacement"
                Stop-Forward
                continue
            } elseif ($r.Code -ne 0) {
                if (-not $clusterWarned) {
                    $clusterWarned = $true
                    Write-Status WARN "cannot check pod $podName right now: $($r.Err) (keeping the tunnel)"
                }
            } else {
                $clusterWarned = $false
                $p = $r.Out | ConvertFrom-Json
                if ($p.metadata.uid -ne $watchedUid -or $p.metadata.deletionTimestamp -or $p.status.phase -ne 'Running') {
                    Write-Status RETRY "pod $podName is terminating or no longer running (phase $($p.status.phase)); reconnecting to its replacement"
                    Stop-Forward
                    continue
                }
                $ready = Get-PodReady $p
                $restarts = Get-PodRestarts $p
                if ($restarts -ne $lastRestarts) {
                    Write-Status WARN "pod $podName container restarted (restarts: $restarts); the tunnel stays on this pod"
                    $lastRestarts = $restarts
                }
                if ($ready -ne $lastReady) {
                    if ($ready) { Write-Status INFO "pod $podName reports Ready again" }
                    else { Write-Status WARN "pod $podName reports not Ready (the app's own state; the tunnel is still up)" }
                    $lastReady = $ready
                }
            }
        }

        if (-not (Wait-Seconds 1)) { break }
    }
} finally {
    Stop-Forward
    if ($script:KeyInput) { try { [Console]::TreatControlCAsInput = $false } catch { } }
    Remove-Item -Recurse -Force -Path $logDir -ErrorAction SilentlyContinue
    Write-Host ''
    Write-Status INFO "browser access stopped; port $LocalPort released. Nothing in the cluster was changed."
}
exit 0
