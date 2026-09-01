param(
  [string]$Name = "brains-quality-gate"
)

if ($PSVersionTable.PSVersion.Major -lt 7) {
  throw "PowerShell 7 or newer is required; run this script with pwsh."
}

$ErrorActionPreference = "Stop"

if ($Name -notmatch "^[a-z0-9][a-z0-9-]{0,62}$") {
  throw "Name must be a lowercase Docker-safe slug."
}

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$image = "${Name}:local"
$container = $Name
$imageOwned = $false
$containerOwned = $false
$succeeded = $false

docker container inspect $container 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
  throw "Refusing to reuse pre-existing container '$container'."
}
docker image inspect $image 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
  throw "Refusing to overwrite pre-existing image '$image'."
}

try {
  $imageOwned = $true
  docker build `
    -f (Join-Path $root "docker\Dockerfile.quality") `
    -t $image `
    $root
  if ($LASTEXITCODE -ne 0) { throw "Quality image build failed." }

  $containerOwned = $true
  docker run --rm `
    --name $container `
    --network none `
    --cap-drop ALL `
    --security-opt "no-new-privileges:true" `
    --tmpfs "/work/.pytest_cache:rw,nosuid,nodev" `
    --tmpfs "/work/.mypy_cache:rw,nosuid,nodev" `
    --tmpfs "/work/.ruff_cache:rw,nosuid,nodev" `
    --tmpfs "/work/dist:rw,nosuid,nodev" `
    --tmpfs "/tmp:rw,exec,nosuid,nodev,mode=1777" `
    $image
  $containerOwned = $false
  if ($LASTEXITCODE -ne 0) { throw "Docker-only quality gate failed." }
  $succeeded = $true
} finally {
  if ($containerOwned) {
    docker rm -f $container 2>$null | Out-Null
  }
  if ($imageOwned) {
    docker image rm $image 2>$null | Out-Null
  }

  docker container inspect $container 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) { throw "Quality container teardown was incomplete." }
  docker image inspect $image 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) { throw "Quality image teardown was incomplete." }
}

if (-not $succeeded) {
  throw "Docker-only quality gate did not complete."
}
