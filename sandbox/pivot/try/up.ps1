# Reproducible local stack for the Brains browser journeys.
# Starts an isolated hub and registers a simulated Runtime. It never launches a
# real agent CLI or reads the operator's ~/.brains state.
param(
  [int]$Port = 8810,
  [string]$Key = "",
  [string]$Name = "trystack"
)

$ErrorActionPreference = "Stop"
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Assert-SafeHarnessName {
  param([string]$Value)

  if ($Value -notmatch "^[a-z0-9][a-z0-9_-]{0,62}$") {
    throw "Harness name must be a lowercase slug containing only letters, digits, hyphens, or underscores."
  }
}

function Get-WorktreeSnapshot {
  param([string]$Repository)

  $lines = New-Object System.Collections.Generic.List[string]
  $head = (& git -C $Repository rev-parse HEAD).Trim()
  if ($LASTEXITCODE -ne 0) {
    throw "Unable to resolve the repository HEAD."
  }
  $lines.Add("head`t$head")

  $status = @(& git -C $Repository status --porcelain=v1 --untracked-files=all)
  if ($LASTEXITCODE -ne 0) {
    throw "Unable to capture repository status."
  }
  foreach ($line in $status) {
    $lines.Add("status`t$line")
  }

  foreach ($relativePath in @(& git -C $Repository ls-files) | Sort-Object) {
    $fullPath = Join-Path $Repository $relativePath
    if (Test-Path $fullPath -PathType Leaf) {
      $hash = (& git -C $Repository hash-object -- $relativePath).Trim()
      if ($LASTEXITCODE -ne 0) {
        throw "Unable to hash tracked file '$relativePath'."
      }
    } else {
      $hash = "<missing>"
    }
    $lines.Add("tracked`t$relativePath`t$hash")
  }

  foreach ($relativePath in @(& git -C $Repository ls-files --others --exclude-standard) | Sort-Object) {
    $hash = (& git -C $Repository hash-object -- $relativePath).Trim()
    if ($LASTEXITCODE -ne 0) {
      throw "Unable to hash untracked file '$relativePath'."
    }
    $lines.Add("untracked`t$relativePath`t$hash")
  }

  foreach ($line in @(& git -C $Repository submodule status --recursive)) {
    $lines.Add("submodule`t$line")
  }
  $lines -join "`n"
}

function Remove-HarnessState {
  param([string]$Path, [string]$Root)

  for ($attempt = 0; $attempt -lt 10 -and (Test-Path $Path); $attempt++) {
    try {
      Remove-Item $Path -Recurse -Force -ErrorAction Stop
    } catch {
      Start-Sleep -Milliseconds 250
    }
  }
  if (Test-Path $Path) {
    throw "Unable to remove state directory '$Path'."
  }
  if ((Test-Path $Root) -and -not (Get-ChildItem $Root -Force -ErrorAction SilentlyContinue)) {
    Remove-Item $Root -Force -ErrorAction SilentlyContinue
  }
}

function Get-DescendantProcessHandles {
  param([int]$ParentId)

  $handles = @()
  $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $ParentId" -ErrorAction SilentlyContinue
  foreach ($child in $children) {
    $childId = [int]$child.ProcessId
    $handles += Get-DescendantProcessHandles -ParentId $childId
    $handle = Get-Process -Id $childId -ErrorAction SilentlyContinue
    if ($handle) {
      [void]$handle.StartTime
      $handles += $handle
    }
  }
  $handles
}

Assert-SafeHarnessName -Value $Name
if ([string]::IsNullOrWhiteSpace($Key)) {
  $Key = $env:BRAINS_E2E_STACK_KEY
}
if ([string]::IsNullOrWhiteSpace($Key)) {
  $Key = "try-brains"
}
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$stateRoot = [System.IO.Path]::GetFullPath((Join-Path $env:TEMP "brains-e2e"))
$state = [System.IO.Path]::GetFullPath((Join-Path $stateRoot $Name))
$statePrefix = $stateRoot.TrimEnd("\") + "\"
if (-not $state.StartsWith($statePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
  throw "Harness state must remain inside '$stateRoot'."
}

$base = "http://127.0.0.1:$Port"
$hub = $null
$stateCreated = $false

try {
  if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "Port $Port is already in use. Refusing to stop an unrelated process."
  }
  if (Test-Path $state) {
    throw "State directory '$state' already exists. Run down.ps1 before starting a new stack."
  }

  New-Item -ItemType Directory -Force $state | Out-Null
  $stateCreated = $true
  $owner = @{
    kind = "brains-e2e-harness"
    name = $Name
    port = $Port
    repo = $repo
    state = $state
  }
  [System.IO.File]::WriteAllText(
    (Join-Path $state "owner.json"),
    ($owner | ConvertTo-Json -Depth 3),
    $utf8
  )
  [System.IO.File]::WriteAllText(
    (Join-Path $state "git-status.before"),
    (Get-WorktreeSnapshot -Repository $repo),
    $utf8
  )
  $configPath = Join-Path $state "brains.yaml"
  [System.IO.File]::WriteAllText($configPath, "{}" + [Environment]::NewLine, $utf8)

  $dbPath = (($state -replace "\\", "/") + "/brains.db")
  $python = Join-Path $repo ".venv\Scripts\python.exe"
  if (-not (Test-Path $python)) {
    $python = (Get-Command python -ErrorAction Stop).Source
  }

  $savedEnvironment = @{}
  $isolatedEnvironment = @(
    Get-ChildItem Env: | Where-Object {
      $_.Name -match "^(BRAINS_|OPENAI_|ANTHROPIC_|AZURE_OPENAI_|GOOGLE_|GEMINI_|LITELLM_|OTEL_|COPILOT_|GH_|UVICORN_|WATCHFILES_|WEB_CONCURRENCY$)"
    }
  )
  foreach ($item in $isolatedEnvironment) {
    $savedEnvironment[$item.Name] = $item.Value
    Remove-Item "Env:$($item.Name)"
  }
  $savedPythonPath = [Environment]::GetEnvironmentVariable("PYTHONPATH", "Process")

  try {
    $env:PYTHONPATH = Join-Path $repo "src"
    $env:BRAINS_CONFIG = $configPath
    $env:BRAINS_RUNTIME_OVERLAY = Join-Path $state "brains.runtime.yaml"
    $env:BRAINS_EXPERIMENTAL_COPILOT_PROVIDER = ""
    $env:BRAINS_ALLOW_COPILOT_PROXY = "0"
    $env:BRAINS_ALLOW_UNAUTHENTICATED_API = "0"
    $env:BRAINS_DUMP_DIR = ""
    $env:BRAINS_STATE_DIR = $state
    $env:BRAINS_DB_URL = "sqlite:///$dbPath"
    $env:BRAINS_API_KEY = $Key
    $env:BRAINS_PREWARM_INDEX_ON_SESSION = "0"
    $env:BRAINS_UI_LABS = "1"

    $hub = Start-Process `
      -FilePath $python `
      -ArgumentList @("-m", "uvicorn", "brains.main:app", "--host", "127.0.0.1", "--port", "$Port", "--workers", "1") `
      -WorkingDirectory $state `
      -PassThru `
      -RedirectStandardOutput (Join-Path $state "hub.out") `
      -RedirectStandardError (Join-Path $state "hub.err") `
      -WindowStyle Hidden
  } finally {
    foreach ($item in @(Get-ChildItem Env: | Where-Object {
      $_.Name -match "^(BRAINS_|OPENAI_|ANTHROPIC_|AZURE_OPENAI_|GOOGLE_|GEMINI_|LITELLM_|OTEL_|COPILOT_|GH_|UVICORN_|WATCHFILES_|WEB_CONCURRENCY$)"
    })) {
      Remove-Item "Env:$($item.Name)"
    }
    foreach ($name in $savedEnvironment.Keys) {
      [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], "Process")
    }
    if ($null -eq $savedPythonPath) {
      Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    } else {
      $env:PYTHONPATH = $savedPythonPath
    }
  }

  $metadata = @{
    pid = $hub.Id
    executable = $python
    start_time_file_utc = $hub.StartTime.ToFileTimeUtc()
    port = $Port
    repo = $repo
    state = $state
  }
  [System.IO.File]::WriteAllText(
    (Join-Path $state "hub.json"),
    ($metadata | ConvertTo-Json -Depth 3),
    $utf8
  )

  function Invoke-BrainsApi {
    param(
      [Parameter(Mandatory = $true)][string]$Method,
      [Parameter(Mandatory = $true)][string]$Path,
      [object]$Body = $null
    )

    $request = @{
      Method = $Method
      Uri = "$base$Path"
      Headers = @{ "x-api-key" = $Key }
      TimeoutSec = 8
    }
    if ($null -ne $Body) {
      $request.ContentType = "application/json"
      $request.Body = $Body | ConvertTo-Json -Depth 8
    }
    Invoke-RestMethod @request
  }

  $healthy = $false
  for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep 1
    if ($hub.HasExited) {
      throw "Hub exited before becoming healthy. See $(Join-Path $state 'hub.err')."
    }
    try {
      if ((Invoke-WebRequest "$base/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) {
        $healthy = $true
        break
      }
    } catch {
      # The hub is still starting.
    }
  }
  if (-not $healthy) {
    throw "Hub did not become healthy. See $(Join-Path $state 'hub.err')."
  }
  if (-not (Test-Path (Join-Path $state "brains.db"))) {
    throw "Hub did not create its database inside the isolated state directory."
  }

  $org = Invoke-BrainsApi POST "/v1/orgs" @{
    slug = "demo"
    name = "Demo Org"
    description = "Try-it workspace"
  }
  if (-not $org.id) {
    throw "Org seed did not return an id."
  }

  $registered = Invoke-BrainsApi POST "/v1/runtimes/register" @{
    machine_id = "e2e-simulated-machine"
    machine_label = "E2E Simulated Runtime"
    org_id = $org.id
    tools = @(
      @{
        tool = "copilot"
        display_name = "Simulated Copilot"
        capabilities = @{
          models = @("claude-sonnet-4.5", "claude-opus-4.8")
          commands = @("simulated")
        }
      }
    )
  }
  $runtime = @($registered.runtimes)[0]
  if (-not $runtime.id) {
    throw "Runtime seed did not return an id."
  }

  Invoke-BrainsApi POST "/v1/orgs/demo/personas" @{
    slug = "mason"
    name = "Mason"
    description = "Backend builder"
    model = "claude-sonnet-4.5"
    tool = "copilot"
    color = "#7c9cff"
    default_runtime_id = $runtime.id
  } | Out-Null
  Invoke-BrainsApi POST "/v1/orgs/demo/personas" @{
    slug = "atelier"
    name = "Atelier"
    description = "Frontend / UI"
    model = "claude-opus-4.8"
    tool = "copilot"
    color = "#f0a8d0"
    default_runtime_id = $runtime.id
  } | Out-Null

  $project = Invoke-BrainsApi POST "/v1/orgs/demo/projects" @{
    slug = "apollo"
    name = "Apollo Launch"
    description = "Ship the operator console"
  }
  if (-not $project.code) {
    throw "Project seed did not return a code."
  }

  $issues = @(
    @{ title = "Design the data model"; status = "done"; priority = "p1" },
    @{ title = "Build the runtime daemon"; status = "done"; priority = "p1" },
    @{ title = "Wire the issues board"; status = "in_progress"; priority = "p1" },
    @{ title = "Polish the config master-detail"; status = "in_progress"; priority = "p2" },
    @{ title = "Add onboarding tour"; status = "open"; priority = "p2" },
    @{ title = "Write the UAT runbook"; status = "in_review"; priority = "p3" },
    @{ title = "Investigate flaky heartbeat"; status = "blocked"; priority = "p1" }
  )
  foreach ($issueSeed in $issues) {
    $issue = Invoke-BrainsApi POST "/v1/projects/$($project.code)/issues" @{
      title = $issueSeed.title
      body = "Demo issue for the board."
      priority = $issueSeed.priority
    }
    if ($issueSeed.status -ne "open") {
      Invoke-BrainsApi POST "/v1/issues/$($issue.code)/transition" @{
        status = $issueSeed.status
      } | Out-Null
    }
  }

  Write-Host ""
  Write-Host "============================================="
  Write-Host " Brains simulated console UP"
  Write-Host "   URL         : $base/app"
  Write-Host "   sign-in key : configured (value hidden)"
  Write-Host "   hub pid     : $($hub.Id)"
  Write-Host "   runtime     : simulated ($($runtime.id))"
  Write-Host "   state dir   : $state"
  Write-Host "============================================="
} catch {
  $failure = $_
  $diagnostics = @()
  foreach ($logName in @("hub.err", "hub.out")) {
    $logPath = Join-Path $state $logName
    if (Test-Path $logPath) {
      $tail = @(Get-Content $logPath -Tail 20 -ErrorAction SilentlyContinue)
      if ($tail.Count -gt 0) {
        $diagnostics += "$logName`:"
        $diagnostics += $tail
      }
    }
  }
  if ($hub -and -not $hub.HasExited) {
    $descendantHandles = @(Get-DescendantProcessHandles -ParentId $hub.Id)
    Stop-Process -InputObject $hub -Force -ErrorAction SilentlyContinue
    foreach ($handle in $descendantHandles) {
      Stop-Process -InputObject $handle -Force -ErrorAction SilentlyContinue
    }
    Wait-Process -InputObject $hub -Timeout 10 -ErrorAction SilentlyContinue
  }
  if ($stateCreated) {
    Remove-HarnessState -Path $state -Root $stateRoot
  }
  $message = $failure.Exception.Message
  if ($diagnostics.Count -gt 0) {
    $message += "`n" + ($diagnostics -join "`n")
  }
  throw $message
}
