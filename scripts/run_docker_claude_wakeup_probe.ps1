param([string]$Name = "brains-claude-wakeup-probe")

$ErrorActionPreference = "Stop"
if ($Name -notmatch "^[a-z0-9][a-z0-9-]{0,48}$") {
  throw "Name must be a lowercase Docker-safe slug no longer than 49 characters."
}

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$status = (& git -C $root status --porcelain=v1 --untracked-files=all) -join "`n"
if ($status) { throw "Claude wakeup probe requires an exact clean committed worktree." }
$commit = (& git -C $root rev-parse HEAD).Trim()
if ($commit -notmatch "^[0-9a-f]{40}$") { throw "Committed renderer identity is unavailable." }
$docker = (Get-Command docker -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
$image = "$Name`:local"
$container = $Name
$createdImage = $false
$createdContainer = $false

function Invoke-DockerQuiet([string[]]$DockerArguments) {
  # Windows PowerShell 5 promotes native stderr to an ErrorRecord when
  # ErrorActionPreference is Stop. Docker inspect reports an absent object on
  # stderr, so suppress that expected stream only for quiet probe operations.
  $savedPreference = $ErrorActionPreference
  try {
    $ErrorActionPreference = "SilentlyContinue"
    & $docker @DockerArguments 2>$null | Out-Null
    return $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $savedPreference
  }
}

try {
  if ((Invoke-DockerQuiet @("container", "inspect", $container)) -eq 0) {
    throw "Refusing to reuse a pre-existing probe container."
  }
  if ((Invoke-DockerQuiet @("image", "inspect", $image)) -eq 0) {
    throw "Refusing to overwrite a pre-existing probe image."
  }

  & $docker build --build-arg "SOURCE_COMMIT=$commit" `
    -f (Join-Path $root "docker\Dockerfile.cli-uat") -t $image $root
  if ($LASTEXITCODE -ne 0) { throw "Claude wakeup probe image build failed." }
  $createdImage = $true

  $createdContainer = $true
  & $docker run --name $container --network none --cap-drop ALL `
    --security-opt no-new-privileges:true `
    --tmpfs "/tmp:rw,exec,nosuid,nodev,mode=1777" `
    --tmpfs "/home/node:rw,exec,nosuid,nodev,uid=1000,gid=1000,mode=0700" `
    $image sh -c 'probe_home=$(mktemp -d /tmp/claude-probe.XXXXXX); HOME="$probe_home" USERPROFILE="$probe_home" /opt/brains-venv/bin/python /opt/uat/claude_wakeup_probe.py'
  if ($LASTEXITCODE -ne 0) { throw "Claude wakeup probe failed." }
} finally {
  if ($createdContainer) {
    if ((Invoke-DockerQuiet @("rm", "-f", $container)) -ne 0) {
      throw "Claude wakeup probe container teardown failed."
    }
  }
  if ($createdImage) {
    if ((Invoke-DockerQuiet @("image", "rm", $image)) -ne 0) {
      throw "Claude wakeup probe image teardown failed."
    }
  }
}
