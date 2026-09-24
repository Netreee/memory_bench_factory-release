$ErrorActionPreference = 'Stop'
$taskPython = Join-Path $PSScriptRoot 'venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    throw 'Python environment is missing. Run: py -3.12 -m venv venv; then venv\Scripts\python.exe -m pip install -r requirements-minimal.txt.'
}
Push-Location $PSScriptRoot
try {
    if ($args.Count -gt 0 -and $args[0] -eq 'agent') {
        $taskAgentArgs = @($args | Select-Object -Skip 1)
        & $taskPython -X utf8 -m tools.agent_pipeline @taskAgentArgs
    }
    else {
        & $taskPython -X utf8 -m pipeline.factory @args
    }
    $taskExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $taskExitCode
