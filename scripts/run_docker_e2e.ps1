param(
  [string]$Name = "brains-mailbox-ui-e2e",
  [string[]]$Spec = @()
)

if ($PSVersionTable.PSVersion.Major -lt 7) {
  throw "PowerShell 7 or newer is required; run this script with pwsh."
}

$ErrorActionPreference = "Stop"

if ($Name -notmatch "^[a-z0-9][a-z0-9-]{0,62}$") {
  throw "Name must be a lowercase Docker-safe slug."
}

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$network = "$Name-net"
$appContainer = "$Name-app"
$browserContainer = "$Name-browser"
$appImage = "$Name-app:local"
$browserImage = "$Name-browser:local"
$key = "docker-e2e-$([Guid]::NewGuid().ToString('N'))"
$appImageCreated = $false
$browserImageCreated = $false
$networkCreated = $false
$appContainerCreated = $false
$browserContainerStarted = $false
$succeeded = $false

function Remove-OwnedArtifacts {
  if ($browserContainerStarted) {
    docker rm -f $browserContainer 2>$null | Out-Null
  }
  if ($appContainerCreated) {
    docker rm -f $appContainer 2>$null | Out-Null
  }
  if ($networkCreated) {
    docker network rm $network 2>$null | Out-Null
  }
  if ($browserImageCreated) {
    docker image rm $browserImage 2>$null | Out-Null
  }
  if ($appImageCreated) {
    docker image rm $appImage 2>$null | Out-Null
  }
}

foreach ($container in @($appContainer, $browserContainer)) {
  docker container inspect $container 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) {
    throw "Refusing to reuse pre-existing container '$container'."
  }
}
docker network inspect $network 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
  throw "Refusing to reuse pre-existing network '$network'."
}
foreach ($image in @($appImage, $browserImage)) {
  docker image inspect $image 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) {
    throw "Refusing to overwrite pre-existing image '$image'."
  }
}

try {
  $appImageCreated = $true
  docker build -t $appImage $root
  if ($LASTEXITCODE -ne 0) { throw "Application image build failed." }

  $browserImageCreated = $true
  docker build -t $browserImage (Join-Path $root "tests\e2e")
  if ($LASTEXITCODE -ne 0) { throw "Playwright image build failed." }

  $networkCreated = $true
  docker network create --internal $network | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "Private Docker network creation failed." }
  if ((docker network inspect --format "{{.Internal}}" $network).Trim() -ne "true") {
    throw "Docker network is not internal."
  }

  $appContainerCreated = $true
  docker run -d `
    --name $appContainer `
    --network $network `
    --cap-drop ALL `
    --security-opt "no-new-privileges:true" `
    --tmpfs "/data:rw,uid=1000,gid=1000,mode=0700" `
    -e "BRAINS_API_KEY=$key" `
    -e "BRAINS_DB_URL=sqlite:////data/brains.db" `
    -e "BRAINS_STATE_DIR=/data/.brains" `
    -e "BRAINS_PREWARM_INDEX_ON_SESSION=0" `
    --entrypoint python `
    $appImage `
    -m uvicorn brains.main:app --host 0.0.0.0 --port 8787 | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "Application container failed to start." }

  $portBindings = (docker inspect --format "{{json .HostConfig.PortBindings}}" $appContainer).Trim()
  if ($portBindings -notin @("null", "{}")) {
    throw "Application container unexpectedly publishes a host port."
  }
  $mountTypes = (docker inspect --format "{{range .Mounts}}{{.Type}} {{end}}" $appContainer).Trim()
  if (($mountTypes -split "\s+") | Where-Object { $_ -and $_ -ne "tmpfs" }) {
    throw "Application container unexpectedly uses a persistent or host mount."
  }

  $healthy = $false
  for ($attempt = 0; $attempt -lt 40; $attempt++) {
    docker exec $appContainer python -c "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=2).status == 200" 2>$null
    if ($LASTEXITCODE -eq 0) {
      $healthy = $true
      break
    }
    Start-Sleep -Seconds 1
  }
  if (-not $healthy) {
    docker logs $appContainer
    throw "Application container did not become healthy."
  }

  docker cp `
    (Join-Path $root "tests\e2e\fixtures\seed_container.py") `
    "${appContainer}:/tmp/seed_container.py"
  if ($LASTEXITCODE -ne 0) { throw "Could not copy the synthetic seed into the container." }

  $manifest = docker exec `
    -e "BRAINS_API_KEY=$key" `
    $appContainer `
    python /tmp/seed_container.py
  if ($LASTEXITCODE -ne 0) { throw "Container seed failed." }
  $manifest = ($manifest -join "`n").Trim()

  $browserContainerStarted = $true
  $testArgs = @("--project=chromium") + $Spec
  docker run --rm `
    --name $browserContainer `
    --network $network `
    --cap-drop ALL `
    --security-opt "no-new-privileges:true" `
    --tmpfs "/tmp:rw,nosuid,nodev,mode=1777" `
    --tmpfs "/work/playwright-report:rw,nosuid,nodev" `
    --tmpfs "/work/test-results:rw,nosuid,nodev" `
    -e "BRAINS_E2E_BASE_URL=http://$appContainer`:8787" `
    -e "BRAINS_E2E_KEY=$key" `
    -e "BRAINS_E2E_SEED_MANIFEST=$manifest" `
    -e "HOME=/tmp" `
    $browserImage `
    $testArgs
  $browserContainerStarted = $false
  if ($LASTEXITCODE -ne 0) { throw "Playwright journey suite failed." }
  $succeeded = $true
} finally {
  Remove-OwnedArtifacts

  foreach ($container in @($appContainer, $browserContainer)) {
    docker container inspect $container 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { throw "Container teardown was incomplete for '$container'." }
  }
  docker network inspect $network 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) { throw "Network teardown was incomplete for '$network'." }
  foreach ($image in @($appImage, $browserImage)) {
    docker image inspect $image 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { throw "Image teardown was incomplete for '$image'." }
  }
}

if (-not $succeeded) {
  throw "Playwright journey suite did not complete."
}
