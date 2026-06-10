$ErrorActionPreference = "Stop"

Push-Location frontend
try {
    npm run check
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
    npm test
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
    npm run build
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
finally {
    Pop-Location
}

$python = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Missing local virtual environment: $python"
}

function Invoke-PythonStep {
    param([string[]]$Arguments)

    & $python @Arguments
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

Invoke-PythonStep -Arguments @("-m", "ruff", "check", ".")
Invoke-PythonStep -Arguments @("-m", "ruff", "format", "--check", ".")
Invoke-PythonStep -Arguments @("-m", "mypy", "probe", "kol_archive", "tests")
Invoke-PythonStep -Arguments @("-m", "pytest")
