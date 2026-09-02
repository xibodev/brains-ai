[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$suffix = "{0}-{1}" -f $PID, ([Guid]::NewGuid().ToString('N').Substring(0, 8))
$qualityImage = "brains-ai-mcp-e2e-quality:$suffix"
$runtimeImage = "brains-ai-mcp-e2e-runtime:$suffix"
$qualityContainer = "brains-ai-mcp-e2e-quality-$suffix"
$runtimeContainer = "brains-ai-mcp-e2e-runtime-$suffix"
$createdQuality = $false
$createdRuntime = $false
$builtQuality = $false
$builtRuntime = $false

try {
    git -C $repoRoot diff --check
    if ($LASTEXITCODE -ne 0) { throw 'git diff --check failed' }

    docker build --file (Join-Path $repoRoot 'docker/Dockerfile.quality') --tag $qualityImage $repoRoot
    if ($LASTEXITCODE -ne 0) { throw 'Docker quality image build failed' }
    $builtQuality = $true

    docker create --name $qualityContainer --network bridge `
        --env HOME=/tmp/brains-e2e-home `
        --env BRAINS_STATE_DIR=/tmp/brains-e2e-state `
        --env BRAINS_DB_URL=sqlite:////tmp/brains-e2e-state/brains.sqlite `
        --env BRAINS_API_KEY=synthetic-docker-e2e-key `
        $qualityImage tail -f /dev/null | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Docker quality container creation failed' }
    $createdQuality = $true

    $isolation = docker inspect --format '{{json .HostConfig.PortBindings}}|{{json .Mounts}}' $qualityContainer
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect Docker isolation' }
    if ($isolation -notin @('null|[]', '{}|[]')) {
        throw "Unsafe Docker isolation: expected no host port bindings or mounts, got $isolation"
    }

    docker start $qualityContainer | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Docker quality container start failed' }

    docker exec $qualityContainer pytest -q `
        tests/test_mcp_transport.py `
        tests/test_mcp_streamable_http.py `
        tests/test_mcp_readiness.py `
        tests/test_mcp_sse_auth.py `
        tests/test_service.py `
        tests/test_supervisor.py `
        tests/test_wire.py `
        tests/test_cli_setup.py
    if ($LASTEXITCODE -ne 0) { throw 'MCP transport, readiness, or wiring tests failed' }

    docker exec $qualityContainer python scripts/docker_codex_mcp_probe.py
    if ($LASTEXITCODE -ne 0) { throw 'Pinned Codex MCP configuration acceptance failed' }

    docker exec $qualityContainer python scripts/check_docs.py
    if ($LASTEXITCODE -ne 0) { throw 'Documentation contract failed' }
    docker exec $qualityContainer python scripts/check_traceability.py
    if ($LASTEXITCODE -ne 0) { throw 'Traceability contract failed' }
    docker exec $qualityContainer ruff check .
    if ($LASTEXITCODE -ne 0) { throw 'Ruff check failed' }
    docker exec $qualityContainer ruff format --check .
    if ($LASTEXITCODE -ne 0) { throw 'Ruff format check failed' }

    docker build --file (Join-Path $repoRoot 'Dockerfile') --tag $runtimeImage $repoRoot
    if ($LASTEXITCODE -ne 0) { throw 'Docker runtime image build failed' }
    $builtRuntime = $true

    docker create --name $runtimeContainer --network bridge `
        --env BRAINS_API_KEY=synthetic-docker-runtime-key `
        $runtimeImage | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Docker runtime container creation failed' }
    $createdRuntime = $true

    $runtimeIsolation = docker inspect --format '{{json .HostConfig.PortBindings}}|{{json .HostConfig.Binds}}|{{json .Mounts}}' $runtimeContainer
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect runtime Docker isolation' }
    $runtimeParts = $runtimeIsolation -split '\|', 3
    if ($runtimeParts[0] -notin @('null', '{}') -or $runtimeParts[1] -notin @('null', '[]')) {
        throw "Unsafe runtime Docker isolation: host ports or bind mounts present: $runtimeIsolation"
    }
    $runtimeMounts = $runtimeParts[2] | ConvertFrom-Json
    if ($runtimeMounts.Count -ne 1 -or
        $runtimeMounts[0].Type -ne 'volume' -or
        $runtimeMounts[0].Destination -ne '/data') {
        throw "Unsafe runtime Docker volume: expected one anonymous synthetic /data volume: $runtimeIsolation"
    }

    docker start $runtimeContainer | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Docker runtime container start failed' }
    $health = 'starting'
    $deadline = [DateTime]::UtcNow.AddSeconds(120)
    while ($health -eq 'starting' -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Seconds 2
        $health = docker inspect --format '{{.State.Health.Status}}' $runtimeContainer
        if ($LASTEXITCODE -ne 0) { throw 'Could not inspect runtime health' }
    }
    if ($health -ne 'healthy') {
        throw "Production image did not become protocol healthy; status=$health"
    }

    Write-Host "Docker MCP acceptance passed; quality isolation $isolation; runtime isolation $runtimeIsolation; health=$health"
}
finally {
    if ($createdRuntime) {
        docker rm --force --volumes $runtimeContainer | Out-Null
    }
    if ($createdQuality) {
        docker rm --force --volumes $qualityContainer | Out-Null
    }
    if ($builtRuntime) {
        docker image rm --force $runtimeImage | Out-Null
    }
    if ($builtQuality) {
        docker image rm --force $qualityImage | Out-Null
    }
}
