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
$image = "$Name`:local"
$container = $Name
$createdImage = $false
$createdContainer = $false

try {
  & docker container inspect $container 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) { throw "Refusing to reuse a pre-existing probe container." }
  & docker image inspect $image 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) { throw "Refusing to overwrite a pre-existing probe image." }

  & docker build --build-arg "SOURCE_COMMIT=$commit" `
    -f (Join-Path $root "docker\Dockerfile.cli-uat") -t $image $root
  if ($LASTEXITCODE -ne 0) { throw "Claude wakeup probe image build failed." }
  $createdImage = $true

  $createdContainer = $true
  & docker run --name $container --network none --cap-drop ALL `
    --security-opt no-new-privileges:true `
    --tmpfs "/tmp:rw,exec,nosuid,nodev,mode=1777" `
    --tmpfs "/home/node:rw,exec,nosuid,nodev,uid=1000,gid=1000,mode=0700" `
    $image sh -lc 'probe_home=$(mktemp -d /tmp/claude-probe.XXXXXX); HOME="$probe_home" USERPROFILE="$probe_home" python /opt/uat/claude_wakeup_probe.py'
  if ($LASTEXITCODE -ne 0) { throw "Claude wakeup probe failed." }
} finally {
  if ($createdContainer) {
    & docker rm -f $container 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Claude wakeup probe container teardown failed." }
  }
  if ($createdImage) {
    & docker image rm $image 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Claude wakeup probe image teardown failed." }
  }
}
