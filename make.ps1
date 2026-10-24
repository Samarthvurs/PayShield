# Windows helper with the same targets as the Makefile. Usage: .\make.ps1 up
param([Parameter(Mandatory = $true)][string]$Target)

$run = @("compose", "run", "--rm", "api")

switch ($Target) {
    "up" {
        if (-not (Test-Path .env)) { Copy-Item .env.example .env }
        docker compose up -d --build
    }
    "down"     { docker compose down }
    "data"     { docker @run python -m scripts.download_data }
    "seed"     { docker @run python -m scripts.seed }
    "train"    { docker @run python -m scripts.train_model }
    "evaluate" { docker @run python -m scripts.evaluate; docker @run python -m scripts.evaluate_rag }
    "ingest"   { docker @run python -m app.ai.ingest }
    "test"     { docker @run pytest --cov=app --cov-report=term-missing }
    "lint"     { docker @run ruff check . }
    "loadtest" { docker @run python -m loadtest.run }
    "demo"     { docker @run python -m scripts.demo_traffic }
    "logs"     { docker compose logs -f api }
    default    { Write-Error "Unknown target: $Target" }
}
