param(
  [string]$Name = "brains-real-cli-mail-uat",
  [string[]]$Tool = @("opencode", "claude"),
  [string]$OutDir = ""
)

if ($PSVersionTable.PSVersion.Major -lt 7) {
  throw "PowerShell 7 or newer is required; run this script with pwsh."
}

$ErrorActionPreference = "Stop"
$allowedTools = @("claude", "copilot", "opencode", "codex")
if ($Name -notmatch "^[a-z0-9][a-z0-9-]{0,48}$") {
  throw "Name must be a lowercase Docker-safe slug no longer than 49 characters."
}
$Tool = @($Tool | Select-Object -Unique)
if ($Tool.Count -lt 2 -or @($Tool | Where-Object { $_ -notin $allowedTools }).Count) {
  throw "Select at least two unique tools from: $($allowedTools -join ', ')."
}

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$appContainer = "$Name-app"
$appImage = "$Name-app:local"
$cliImage = "$Name-cli:local"
$controlNetwork = "$Name-control"
$egressNetwork = "$Name-egress"
$stateVolume = "$Name-state"
$key = "docker-cli-uat-$([Guid]::NewGuid().ToString('N'))"
$mcpTools = "start_session,end_session,mailbox_send,mailbox_inbox,mailbox_reply"
$createdContainers = [System.Collections.Generic.List[string]]::new()
$appImageCreated = $false
$cliImageCreated = $false
$controlNetworkCreated = $false
$egressNetworkCreated = $false
$stateVolumeCreated = $false
$report = [ordered]@{
  status = "running"
  base_commit = (& git rev-parse HEAD).Trim()
  candidate_diff_hash = (& git diff --no-ext-diff --binary HEAD | & git hash-object --stdin).Trim()
  platform = "docker-linux"
  tools = @()
  retries = @()
  scenarios = [ordered]@{}
  isolation = [ordered]@{
    published_ports = 0
    app_egress = $false
    cli_credentials = "separate read-only runtime mounts copied only into disposable homes when a CLI requires writes"
    source_mounts = 0
    state = "disposable owned Docker volume"
    mcp_transport = "candidate stdio servers over one shared disposable SQLite store"
  }
  teardown_verified = $false
}
$failure = $null

if (-not $OutDir) {
  $OutDir = Join-Path ([IO.Path]::GetTempPath()) "$Name-$([Guid]::NewGuid().ToString('N'))"
}
$reportParent = Split-Path -Parent $OutDir
if (-not (Test-Path -LiteralPath $reportParent)) {
  throw "Report parent directory does not exist."
}
if (Test-Path -LiteralPath $OutDir) {
  throw "Refusing to reuse pre-existing report directory."
}
[IO.Directory]::CreateDirectory($OutDir) | Out-Null
$reportPath = Join-Path $OutDir "report.json"
$appLogPath = Join-Path $OutDir "brains.log"

function ConvertTo-Base64([string]$Value) {
  return [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Value))
}

function Assert-ArtifactAbsent([string]$Kind, [string]$Value) {
  & docker $Kind inspect $Value 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) {
    throw "Refusing to reuse pre-existing Docker $Kind '$Value'."
  }
}

function Invoke-Actor([string]$ActorTool, [string[]]$Arguments) {
  $container = "$Name-$ActorTool"
  $output = @(& docker exec $container python /opt/uat/real_cli_actor.py @Arguments 2>&1)
  $exitCode = $LASTEXITCODE
  $payload = $null
  for ($index = $output.Count - 1; $index -ge 0; $index--) {
    try {
      $candidate = $output[$index] | ConvertFrom-Json
      if ($null -ne $candidate.ok) { $payload = $candidate; break }
    } catch {}
  }
  if ($null -eq $payload) {
    throw "$ActorTool UAT actor returned an invalid machine result."
  }
  if ($exitCode -ne 0 -or -not $payload.ok) {
    $script:report["last_actor_failure"] = [ordered]@{
      tool = $payload.tool
      failure_category = $payload.failure_category
      diagnostic_codes = @($payload.diagnostic_codes)
      return_code = $payload.return_code
      event_types = @($payload.event_types)
    }
    $category = if ($payload.failure_category) { $payload.failure_category } else { "unknown" }
    throw "$ActorTool UAT actor failed in category '$category'."
  }
  return $payload
}

function Invoke-Turn(
  [string]$ActorTool,
  [string]$Prompt,
  [string]$SessionId = "",
  [string]$Expected = "",
  [int]$Attempts = 1,
  [string]$Stage = "turn"
) {
  $arguments = @("run", "--tool", $ActorTool, "--prompt-b64", (ConvertTo-Base64 $Prompt))
  if ($SessionId) { $arguments += @("--session-id", $SessionId) }
  if ($Expected) { $arguments += @("--expected-b64", (ConvertTo-Base64 $Expected)) }
  for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
    try {
      $result = Invoke-Actor $ActorTool $arguments
      if ($attempt -gt 1) {
        $script:report["last_actor_failure"] = $null
      }
      return $result
    } catch {
      if ($attempt -ge $Attempts) { throw }
      $script:report["retries"] = @($script:report["retries"]) + @([ordered]@{
        tool = $ActorTool
        stage = $Stage
        failed_attempt = $attempt
        maximum_attempts = $Attempts
      })
      Start-Sleep -Seconds 2
    }
  }
}

function Invoke-Inspection([string]$ActorTool, [hashtable]$Request) {
  $json = $Request | ConvertTo-Json -Compress -Depth 8
  return Invoke-Actor $ActorTool @("inspect", "--request-b64", (ConvertTo-Base64 $json))
}

function Get-CredentialMounts([string]$ActorTool) {
  $homePath = [Environment]::GetFolderPath("UserProfile")
  $primary = switch ($ActorTool) {
    "claude" { Join-Path $homePath ".claude\.credentials.json" }
    "copilot" { Join-Path $homePath ".copilot\config.json" }
    "opencode" { Join-Path $homePath ".local\share\opencode\auth.json" }
    "codex" { Join-Path $homePath ".codex\auth.json" }
  }
  if (-not (Test-Path -LiteralPath $primary -PathType Leaf)) {
    throw "$ActorTool credential source is unavailable."
  }
  $mounts = @("--mount", "type=bind,source=$primary,target=/run/credentials/primary,readonly")
  return $mounts
}

$beforeStatus = (& git status --porcelain=v1 --untracked-files=all) -join "`n"
$beforeDiff = (& git diff --no-ext-diff --binary HEAD | & git hash-object --stdin).Trim()
$beforeUntracked = @(
  & git ls-files --others --exclude-standard | ForEach-Object {
    "$_`t$((& git hash-object -- $_).Trim())"
  }
) -join "`n"
$fingerprintMaterial = "$beforeStatus`n$beforeDiff`n$beforeUntracked"
$report.candidate_worktree_hash = ($fingerprintMaterial | & git hash-object --stdin).Trim()

try {
  Assert-ArtifactAbsent "container" $appContainer
  foreach ($item in $Tool) { Assert-ArtifactAbsent "container" "$Name-$item" }
  Assert-ArtifactAbsent "image" $appImage
  Assert-ArtifactAbsent "image" $cliImage
  Assert-ArtifactAbsent "network" $controlNetwork
  Assert-ArtifactAbsent "network" $egressNetwork
  Assert-ArtifactAbsent "volume" $stateVolume

  $appImageCreated = $true
  & docker build -t $appImage $root
  if ($LASTEXITCODE -ne 0) { throw "Brains application image build failed." }
  $cliImageCreated = $true
  & docker build -f (Join-Path $root "docker\Dockerfile.cli-uat") -t $cliImage $root
  if ($LASTEXITCODE -ne 0) { throw "Real-CLI UAT image build failed." }
  $report.app_image = (& docker image inspect $appImage --format "{{.Id}}").Trim()
  $report.cli_image = (& docker image inspect $cliImage --format "{{.Id}}").Trim()

  $controlNetworkCreated = $true
  & docker network create --internal $controlNetwork | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "Internal control network creation failed." }
  $egressNetworkCreated = $true
  & docker network create $egressNetwork | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "CLI egress network creation failed." }
  if ((& docker network inspect --format "{{.Internal}}" $controlNetwork).Trim() -ne "true") {
    throw "Control network is not internal."
  }
  if ((& docker network inspect --format "{{.Internal}}" $egressNetwork).Trim() -ne "false") {
    throw "CLI egress network is unexpectedly internal."
  }

  $stateVolumeCreated = $true
  & docker volume create $stateVolume | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "Disposable state volume creation failed." }

  $appArgs = @(
    "run", "-d", "--name", $appContainer,
    "--network", $controlNetwork,
    "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges:true",
    "--mount", "type=volume,source=$stateVolume,target=/data",
    "--tmpfs", "/tmp:rw,exec,nosuid,nodev,mode=1777",
    "--tmpfs", "/workspace:rw,uid=1000,gid=1000,mode=0700",
    "-e", "BRAINS_API_KEY=$key",
    "-e", "BRAINS_DB_URL=sqlite:////data/brains.db",
    "-e", "BRAINS_STATE_DIR=/data/.brains",
    "-e", "BRAINS_PREWARM_INDEX_ON_SESSION=0",
    "-e", "BRAINS_MCP_BIND=0.0.0.0",
    "-e", "BRAINS_MCP_ALLOW_PUBLIC=1",
    "-e", "BRAINS_MCP_TOOLS=$mcpTools",
    $appImage,
    "serve-all", "--gateway-host", "0.0.0.0"
  )
  & docker @appArgs | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "Brains UAT container failed to start." }
  $createdContainers.Add($appContainer)
  $portBindings = (& docker inspect --format "{{json .HostConfig.PortBindings}}" $appContainer).Trim()
  if ($portBindings -notin @("null", "{}")) { throw "Brains UAT published a host port." }

  $healthy = $false
  for ($attempt = 0; $attempt -lt 60; $attempt++) {
    & docker exec $appContainer python -c "import socket,urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=2).status == 200; socket.create_connection(('127.0.0.1',9877), timeout=2).close()" 2>$null
    if ($LASTEXITCODE -eq 0) { $healthy = $true; break }
    Start-Sleep -Seconds 1
  }
  if (-not $healthy) { throw "Brains UAT container did not become healthy." }
  & docker exec $appContainer mkdir -p /workspace/uat
  if ($LASTEXITCODE -ne 0) { throw "Brains UAT workspace setup failed." }
  $report.scenarios.brains_serve_all_healthy = $true

  foreach ($item in $Tool) {
    $container = "$Name-$item"
    $actorArgs = @(
      "run", "-d", "--name", $container,
      "--network", $controlNetwork,
      "--cap-drop", "ALL",
      "--security-opt", "no-new-privileges:true",
      "--mount", "type=volume,source=$stateVolume,target=/data",
      "--tmpfs", "/home/node:rw,exec,nosuid,nodev,uid=1000,gid=1000,mode=0700",
      "--tmpfs", "/workspace:rw,uid=1000,gid=1000,mode=0700",
      "--tmpfs", "/tmp:rw,exec,nosuid,nodev,mode=1777",
      "-e", "BRAINS_DB_URL=sqlite:////data/brains.db",
      "-e", "BRAINS_STATE_DIR=/data/.brains",
      "-e", "BRAINS_PREWARM_INDEX_ON_SESSION=0"
    )
    $actorArgs += Get-CredentialMounts $item
    $actorArgs += @($cliImage, "sleep", "infinity")
    & docker @actorArgs | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "$item UAT container failed to start." }
    $createdContainers.Add($container)
    & docker network connect $egressNetwork $container
    if ($LASTEXITCODE -ne 0) { throw "$item UAT egress attachment failed." }
    $portBindings = (& docker inspect --format "{{json .HostConfig.PortBindings}}" $container).Trim()
    if ($portBindings -notin @("null", "{}")) { throw "$item UAT published a host port." }
    $mounts = (& docker inspect --format "{{range .Mounts}}{{.Type}}:{{.Destination}}:{{.RW}} {{end}}" $container).Trim()
    if ($mounts -match "bind:/run/credentials/[^:]*:true") {
      throw "$item credential mount is writable."
    }
  }

  $tokens = @{ claude = "PELICAN"; copilot = "KIWI"; opencode = "MANGO"; codex = "ZEBRA" }
  $states = @{}
  foreach ($item in $Tool) {
    $configured = Invoke-Actor $item @("configure", "--tool", $item)
    $bootstrap = Invoke-Turn $item "Reply with exactly $($tokens[$item]) and nothing else. Do not call any tool." "" $tokens[$item]
    if (-not $bootstrap.native_session_id) { throw "$item did not expose its native Session ID." }
    $binding = Invoke-Actor $item @("binding")
    $canonical = switch ($item) {
      "claude" { "claude-code" }
      "copilot" { "copilot-cli" }
      default { $item }
    }
    $registerPrompt = @"
Use only the Brains MCP server. Call brains_start_session exactly once with workspace_path "/workspace/uat", tool "$canonical", native_tool_session_id "$($bootstrap.native_session_id)", mailbox_binding_file "$($binding.binding_file)", and mailbox_notification_mode "pull". Do not call shell, file, web, or any other tool. After success, repeat the exact one-word token from the previous turn followed by REGISTERED.
"@
    $registeredTurn = Invoke-Turn $item $registerPrompt $bootstrap.native_session_id "$($tokens[$item]) REGISTERED" 2 "mailbox_registration"
    $registration = Invoke-Inspection $item @{
      kind = "registration"
      tool = $item
      native_session_id = $bootstrap.native_session_id
    }
    $states[$item] = [ordered]@{
      native = $bootstrap.native_session_id
      binding = $binding.binding_file
      address = $registration.address
      mailbox_id = $registration.mailbox_id
      brain = $registration.brain_session_id
      event_types = @($bootstrap.event_types + $registeredTurn.event_types | Select-Object -Unique)
    }
    $report.tools += [ordered]@{
      tool = $item
      version = $configured.version
      token = $tokens[$item]
      native_id_extracted = $true
      conversation_resumed = $registeredTurn.expected_text_seen
      mailbox_registered = $true
      mcp_transport = $configured.transport
    }
  }
  $report.scenarios.real_native_ids_and_resume = $true
  $report.scenarios.real_cli_mailbox_registration = $true

  $senderTool = $Tool[0]
  $recipientTools = @($Tool | Select-Object -Skip 1)
  foreach ($item in $recipientTools) {
    $offlinePrompt = "Use only Brains. Call brains_end_session with session_id `"$($states[$item].brain)`" and summary `"real-CLI UAT offline boundary`". After success reply exactly OFFLINE."
    Invoke-Turn $item $offlinePrompt $states[$item].native "OFFLINE" | Out-Null
    Invoke-Inspection $item @{ kind = "ended"; brain_session_id = $states[$item].brain } | Out-Null
  }
  $report.scenarios.recipients_detached_before_delivery = $true

  $subject = "real-cli-uat-$([Guid]::NewGuid().ToString('N'))"
  $body = "synthetic durable mailbox payload $([Guid]::NewGuid().ToString('N'))"
  $operation = "uat-send-$([Guid]::NewGuid().ToString('N'))"
  $recipientAddresses = @($recipientTools | ForEach-Object { $states[$_].address })
  $recipientJson = ConvertTo-Json -InputObject @($recipientAddresses) -Compress
  $sendPrompt = @"
Use only Brains. Call brains_mailbox_send exactly once with workspace_path "/workspace/uat", recipients $recipientJson, subject "$subject", body "$body", operation_id "$operation", sender_session_id "$($states[$senderTool].brain)", binding_file "$($states[$senderTool].binding)", and kind "info". After local acceptance reply exactly SENT.
"@
  Invoke-Turn $senderTool $sendPrompt $states[$senderTool].native "SENT" | Out-Null
  $message = Invoke-Inspection $senderTool @{
    kind = "message"
    subject = $subject
    recipient_count = $recipientTools.Count
  }
  $report.scenarios.offline_multi_recipient_local_acceptance = $true

  $replyIds = @()
  foreach ($item in $recipientTools) {
    $replySubject = "reply-$item-$([Guid]::NewGuid().ToString('N'))"
    $replyBody = "synthetic reply from $item $([Guid]::NewGuid().ToString('N'))"
    $replyOperation = "uat-reply-$item-$([Guid]::NewGuid().ToString('N'))"
    $recoverPrompt = @"
Use only Brains. First call brains_start_session with workspace_path "/workspace/uat", tool "$(switch ($item) { 'claude' { 'claude-code' } 'copilot' { 'copilot-cli' } default { $item } })", predecessor_session_id "$($states[$item].brain)", native_tool_session_id "$($states[$item].native)", mailbox_binding_file "$($states[$item].binding)", and mailbox_notification_mode "pull". Then call brains_mailbox_inbox with the returned session_id, binding_file "$($states[$item].binding)", mark_read true, include_read false, and limit 10. Find message_id "$($message.message_id)" and call brains_mailbox_reply with workspace_path "/workspace/uat", in_reply_to "$($message.message_id)", operation_id "$replyOperation", sender_session_id set to the new Brains session_id, binding_file "$($states[$item].binding)", subject "$replySubject", body "$replyBody", and kind "info". After success reply exactly RECOVERED.
"@
    Invoke-Turn $item $recoverPrompt $states[$item].native "RECOVERED" | Out-Null
    $recovery = Invoke-Inspection $item @{
      kind = "recovery"
      tool = $item
      native_session_id = $states[$item].native
      old_brain_session_id = $states[$item].brain
      message_id = $message.message_id
      reply_subject = $replySubject
    }
    $states[$item].brain = $recovery.brain_session_id
    $replyIds += $recovery.reply_message_id
  }
  $report.scenarios.same_cli_session_successor_recovery = $true
  $report.scenarios.cross_cli_threaded_replies = $true

  $replyJson = ConvertTo-Json -InputObject @($replyIds) -Compress
  $readPrompt = "Use only Brains. Call brains_mailbox_inbox with session_id `"$($states[$senderTool].brain)`", binding_file `"$($states[$senderTool].binding)`", mark_read true, include_read false, and limit 20. Confirm that all reply message IDs in $replyJson were returned, then reply exactly COMPLETE."
  Invoke-Turn $senderTool $readPrompt $states[$senderTool].native "COMPLETE" | Out-Null
  Invoke-Inspection $senderTool @{
    kind = "sender_read"
    tool = $senderTool
    native_session_id = $states[$senderTool].native
    reply_message_ids = $replyIds
  } | Out-Null
  $report.scenarios.sender_read_all_replies = $true

  $afterStatus = (& git status --porcelain=v1 --untracked-files=all) -join "`n"
  $afterDiff = (& git diff --no-ext-diff --binary HEAD | & git hash-object --stdin).Trim()
  $afterUntracked = @(
    & git ls-files --others --exclude-standard | ForEach-Object {
      "$_`t$((& git hash-object -- $_).Trim())"
    }
  ) -join "`n"
  if ($beforeStatus -ne $afterStatus -or $beforeDiff -ne $afterDiff -or $beforeUntracked -ne $afterUntracked) {
    throw "Real-CLI UAT changed the candidate worktree."
  }
  $report.scenarios.candidate_worktree_unchanged = $true
  $report.status = "passed"
} catch {
  $failure = $_.Exception.Message
  $report.status = "failed"
  $report.failure = $failure
} finally {
  if ($createdContainers.Contains($appContainer)) {
    $logs = & docker logs $appContainer 2>&1
    [IO.File]::WriteAllLines($appLogPath, [string[]]$logs)
  }
  for ($index = $createdContainers.Count - 1; $index -ge 0; $index--) {
    & docker rm -f $createdContainers[$index] 2>$null | Out-Null
  }
  if ($controlNetworkCreated) { & docker network rm $controlNetwork 2>$null | Out-Null }
  if ($egressNetworkCreated) { & docker network rm $egressNetwork 2>$null | Out-Null }
  if ($stateVolumeCreated) { & docker volume rm $stateVolume 2>$null | Out-Null }
  if ($cliImageCreated) { & docker image rm $cliImage 2>$null | Out-Null }
  if ($appImageCreated) { & docker image rm $appImage 2>$null | Out-Null }

  $teardownOk = $true
  foreach ($container in @($appContainer) + @($Tool | ForEach-Object { "$Name-$_" })) {
    & docker container inspect $container 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $teardownOk = $false }
  }
  foreach ($network in @($controlNetwork, $egressNetwork)) {
    & docker network inspect $network 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $teardownOk = $false }
  }
  & docker volume inspect $stateVolume 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) { $teardownOk = $false }
  foreach ($image in @($appImage, $cliImage)) {
    & docker image inspect $image 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $teardownOk = $false }
  }
  $report.teardown_verified = $teardownOk
  [IO.File]::WriteAllText($reportPath, (($report | ConvertTo-Json -Depth 10) + "`n"))
}

if (-not $report.teardown_verified) {
  throw "Real-CLI UAT Docker teardown was incomplete. Report: $reportPath"
}
if ($failure) {
  throw "Real-CLI UAT failed: $failure Report: $reportPath"
}
"Real-CLI durable-mail UAT passed. Report: $reportPath"
