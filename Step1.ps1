[CmdletBinding()]
param(
    # Mod root folder. Defaults to this script's own folder so the original
    # double-click-to-run standalone workflow still works unchanged.
    [string]$ModRoot = (Split-Path -Parent $MyInvocation.MyCommand.Definition),
    [string]$ExampleConfigPath
)

$ErrorActionPreference = 'Stop'
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition

# Accept either <ModRoot>\Textures\PBR (the classic layout) or, if that
# doesn't exist, treat ModRoot itself as the PBR root — this matches the
# Python-side tool, which doesn't require any specific subfolder structure.
$rootFolder = Join-Path $ModRoot "Textures\PBR"
if (-not (Test-Path -LiteralPath $rootFolder)) {
    $rootFolder = $ModRoot
}
$outputFolder = Join-Path $ModRoot "PBRNifPatcher"

if (-not $ExampleConfigPath) {
    $ExampleConfigPath = Join-Path $scriptRoot "ExampleConfig.json"
}

if (-not (Test-Path -LiteralPath $rootFolder)) {
    Write-Host "[ERROR] Mod folder does not exist: $ModRoot"
    exit 1
}
if (-not (Test-Path -LiteralPath $ExampleConfigPath)) {
    Write-Host "[ERROR] Missing template config: $ExampleConfigPath"
    exit 1
}

if (-not (Test-Path -LiteralPath $outputFolder)) {
    New-Item -ItemType Directory -Path $outputFolder | Out-Null
}

$logSuccess = @()
$logFailed  = @()

$allDDS = Get-ChildItem -LiteralPath $rootFolder -Recurse -Filter *.dds -ErrorAction SilentlyContinue
$diffuseFiles = $allDDS | Where-Object {
    $_.Name -notmatch '(_n|_g|_s|_cnr|_f|_p|_rmaos)\.dds$'
}

$total = $diffuseFiles.Count
Write-Host "__SKYKING_TOTAL__=$total"

if ($total -eq 0) {
    Write-Host "[WARN] No un-suffixed diffuse .dds files found under: $rootFolder"
    Write-Host "[DONE] Step 1 complete. Nothing to scaffold."
    exit 0
}

$done = 0

foreach ($ddsFile in $diffuseFiles) {
    try {
        $relativePath = $ddsFile.DirectoryName.Substring($rootFolder.Length).TrimStart('\')

        $targetDir = Join-Path $outputFolder $relativePath
        if (-not (Test-Path -LiteralPath $targetDir)) {
            New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
        }

        $baseName = $ddsFile.BaseName
        $newConfigPath = Join-Path $targetDir "$baseName.json"

        Copy-Item -LiteralPath $ExampleConfigPath -Destination $newConfigPath -Force

        $logSuccess += "Successfully created config file: $newConfigPath"
        Write-Host "Scaffolded $baseName"
    }
    catch {
        $logFailed += "Failed to create config file for: $($ddsFile.FullName) - $($_.Exception.Message)"
        Write-Host "[ERROR] Failed: $($ddsFile.FullName) - $($_.Exception.Message)"
    }
    $done++
    Write-Host "__SKYKING_PROGRESS__=$done/$total"
}

$logFilePath = Join-Path $ModRoot "PBR Json Generator.txt"
$logSuccess + $logFailed | Out-File -FilePath $logFilePath -Encoding UTF8

Write-Host "[DONE] Step 1 complete. Log file: $logFilePath"
exit 0
