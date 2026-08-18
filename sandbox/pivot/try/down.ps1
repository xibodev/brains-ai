# Stop the exact isolated hub process tree, verify the worktree is unchanged,
# and remove the owned temporary state directory.
param([string]$Name = "trystack", [int]$Port = 8810)

$ErrorActionPreference = "Stop"

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

Assert-SafeHarnessName -Value $Name
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$stateRoot = [System.IO.Path]::GetFullPath((Join-Path $env:TEMP "brains-e2e"))
$state = [System.IO.Path]::GetFullPath((Join-Path $stateRoot $Name))
$statePrefix = $stateRoot.TrimEnd("\") + "\"
if (-not $state.StartsWith($statePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
  throw "Harness state must remain inside '$stateRoot'."
}

if (-not (Test-Path $state)) {
  if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "No owned harness state exists, but port $Port is in use. Refusing to stop an unrelated process."
  }
  Write-Host "stack '$Name' is already down."
  return
}

$ownerPath = Join-Path $state "owner.json"
if (-not (Test-Path $ownerPath)) {
  throw "State directory '$state' has no Brains E2E ownership marker; refusing to delete it."
}
$owner = Get-Content $ownerPath -Raw | ConvertFrom-Json
if (
  $owner.kind -ne "brains-e2e-harness" -or
  $owner.name -ne $Name -or
  [int]$owner.port -ne $Port -or
  [System.IO.Path]::GetFullPath([string]$owner.repo) -ne $repo -or
  [System.IO.Path]::GetFullPath([string]$owner.state) -ne $state
) {
  throw "State ownership metadata does not match this repository, name, port, and path."
}

$metadataPath = Join-Path $state "hub.json"
if (Test-Path $metadataPath) {
  $metadata = Get-Content $metadataPath -Raw | ConvertFrom-Json
  if (
    [int]$metadata.port -ne $Port -or
    [System.IO.Path]::GetFullPath([string]$metadata.repo) -ne $repo -or
    [System.IO.Path]::GetFullPath([string]$metadata.state) -ne $state
  ) {
    throw "Hub metadata does not match this repository, port, and state path."
  }

  $processId = [int]$metadata.pid
  $process = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
  if ($process) {
    $hubHandle = Get-Process -Id $processId -ErrorAction Stop
    $commandLine = [string]$process.CommandLine
    $expectedExecutable = [System.IO.Path]::GetFullPath([string]$metadata.executable)
    $actualExecutable = [System.IO.Path]::GetFullPath([string]$process.ExecutablePath)
    if ($actualExecutable -ne $expectedExecutable) {
      throw "PID $processId executable does not match the recorded harness process."
    }
    if ($hubHandle.StartTime.ToFileTimeUtc() -ne [int64]$metadata.start_time_file_utc) {
      throw "PID $processId start time does not match the recorded harness process."
    }
    if ($commandLine -notmatch "uvicorn\s+brains\.main:app" -or $commandLine -notmatch "--port\s+$Port") {
      throw "PID $processId command line does not match the recorded Brains hub."
    }

    $descendantHandles = @(Get-DescendantProcessHandles -ParentId $processId)
    Stop-Process -InputObject $hubHandle -Force -ErrorAction Stop
    foreach ($handle in $descendantHandles) {
      Stop-Process -InputObject $handle -Force -ErrorAction SilentlyContinue
    }
    Wait-Process -InputObject $hubHandle -Timeout 10 -ErrorAction SilentlyContinue
    if (-not $hubHandle.HasExited) {
      throw "Harness process $processId did not exit."
    }
    foreach ($handle in $descendantHandles) {
      $handle.Refresh()
      if (-not $handle.HasExited) {
        throw "Harness descendant process $($handle.Id) did not exit."
      }
    }
    Write-Host "stopped hub process tree -> $processId"
  }
}

$listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
if ($listeners.Count -gt 0) {
  $owners = ($listeners | ForEach-Object { $_.OwningProcess } | Sort-Object -Unique) -join ", "
  throw "Port $Port is still in use by PID(s) $owners; refusing to stop an unverified process."
}

$mutationMessage = $null
$baselinePath = Join-Path $state "git-status.before"
if (Test-Path $baselinePath) {
  $before = [System.IO.File]::ReadAllText($baselinePath)
  $after = Get-WorktreeSnapshot -Repository $repo
  if ($after -cne $before) {
    $mutationMessage = "E2E changed the repository worktree contents or Git state."
  }
}

Remove-HarnessState -Path $state -Root $stateRoot
if ($mutationMessage) {
  throw $mutationMessage
}

Write-Host "stack '$Name' down; process tree stopped, worktree unchanged, state removed."
