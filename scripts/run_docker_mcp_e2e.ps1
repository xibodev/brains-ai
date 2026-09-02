[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$suffix = "{0}-{1}" -f $PID, ([Guid]::NewGuid().ToString('N').Substring(0, 8))
$image = "brains-ai-mcp-e2e:$suffix"
$container = "brains-ai-mcp-e2e-$suffix"
$created = $false
$built = $false

try {
    git -C $repoRoot diff --check
    if ($LASTEXITCODE -ne 0) { throw 'git diff --check failed' }

    docker build --file (Join-Path $repoRoot 'docker/Dockerfile.quality') --tag $image $repoRoot
    if ($LASTEXITCODE -ne 0) { throw 'Docker image build failed' }
    $built = $true

    docker create --name $container --network bridge `
        --env HOME=/tmp/brains-e2e-home `
        --env BRAINS_STATE_DIR=/tmp/brains-e2e-state `
        --env BRAINS_DB_URL=sqlite:////tmp/brains-e2e-state/brains.sqlite `
        --env BRAINS_API_KEY=synthetic-docker-e2e-key `
        $image tail -f /dev/null | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Docker container creation failed' }
    $created = $true

    $isolation = docker inspect --format '{{json .HostConfig.PortBindings}}|{{json .Mounts}}' $container
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect Docker isolation' }
    if ($isolation -notin @('null|[]', '{}|[]')) {
        throw "Unsafe Docker isolation: expected no host port bindings or mounts, got $isolation"
    }

    docker start $container | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Docker container start failed' }

    docker exec $container pytest -q `
        tests/test_mcp_transport.py `
        tests/test_mcp_streamable_http.py `
        tests/test_mcp_readiness.py `
        tests/test_mcp_sse_auth.py `
        tests/test_service.py `
        tests/test_supervisor.py `
        tests/test_wire.py `
        tests/test_cli_setup.py
    if ($LASTEXITCODE -ne 0) { throw 'MCP transport, readiness, or wiring tests failed' }

    docker exec $container python scripts/check_docs.py
    if ($LASTEXITCODE -ne 0) { throw 'Documentation contract failed' }
    docker exec $container python scripts/check_traceability.py
    if ($LASTEXITCODE -ne 0) { throw 'Traceability contract failed' }
    docker exec $container ruff check .
    if ($LASTEXITCODE -ne 0) { throw 'Ruff check failed' }
    docker exec $container ruff format --check .
    if ($LASTEXITCODE -ne 0) { throw 'Ruff format check failed' }

    Write-Host "Docker MCP acceptance passed with isolation $isolation"
}
finally {
    if ($created) {
        docker rm --force $container | Out-Null
    }
    if ($built) {
        docker image rm --force $image | Out-Null
    }
}
