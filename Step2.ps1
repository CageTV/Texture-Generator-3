[CmdletBinding()]
param(
    [string]$ModRoot = (Split-Path -Parent $MyInvocation.MyCommand.Path),

    # Tunable defaults - override any of these from the GUI without editing the script.
    [double]$SpecularLevel      = 0.04,
    [double]$RoughnessScale     = 1.0,
    [double]$SubsurfaceOpacity  = 1.0,
    [double]$DisplacementScale  = 1.0,
    [double]$MultilayerDisplacementScale = 2.0,
    [double]$CoatStrength       = 1.0,
    [double]$CoatRoughness      = 1.0,
    [double]$CoatSpecularLevel  = 0.018
)

$ErrorActionPreference = 'Stop'

$pbr_folder = Join-Path $ModRoot "pbrnifpatcher"
if (-not (Test-Path -LiteralPath $pbr_folder)) {
    $pbr_folder = Join-Path $ModRoot "PBRNifPatcher"
}

# Accept either <ModRoot>\Textures\PBR (the classic layout) or, if that
# doesn't exist, treat ModRoot itself as the map root — matches Step1.ps1
# and the Python-side tool, which don't require a specific subfolder layout.
$textures_pbr_folder = Join-Path $ModRoot "Textures\PBR"
if (-not (Test-Path -LiteralPath $textures_pbr_folder)) {
    $textures_pbr_folder = $ModRoot
}

if (-not (Test-Path -LiteralPath $pbr_folder)) {
    Write-Host "[ERROR] Missing pbrnifpatcher under: $ModRoot (run Step 1 first)"
    exit 1
}
if (-not (Test-Path -LiteralPath $textures_pbr_folder)) {
    Write-Host "[ERROR] Mod folder does not exist: $ModRoot"
    exit 1
}

$json_files = Get-ChildItem -LiteralPath $pbr_folder -Filter *.json -Recurse -ErrorAction SilentlyContinue
if (-not $json_files -or $json_files.Count -eq 0) {
    Write-Host "[ERROR] No .json files found under $pbr_folder. Run Step 1 first."
    exit 1
}

$total = $json_files.Count
Write-Host "__SKYKING_TOTAL__=$total"
$done = 0

foreach ($file in $json_files) {
    try {
        $json_name  = $file.BaseName
        $is_renamed = $json_name -like "*_d"
        $base_name  = if ($is_renamed) { $json_name -replace "_d$" } else { $json_name }

        $json_dir = Split-Path -Parent $file.FullName

        $relative_path = $json_dir.Substring($pbr_folder.Length).TrimStart('\')

        if ($relative_path) {
            $map_folder = Join-Path $textures_pbr_folder $relative_path
        } else {
            $map_folder = $textures_pbr_folder
        }

        if (-not (Test-Path -LiteralPath $map_folder)) {
            $map_folder = $textures_pbr_folder
        }

        $d_map   = Get-ChildItem -LiteralPath $map_folder -Filter "${base_name}_d.dds"   -ErrorAction SilentlyContinue
        $g_map   = Get-ChildItem -LiteralPath $map_folder -Filter "${base_name}_g.dds"   -ErrorAction SilentlyContinue
        $f_map   = Get-ChildItem -LiteralPath $map_folder -Filter "${base_name}_f.dds"   -ErrorAction SilentlyContinue
        $p_map   = Get-ChildItem -LiteralPath $map_folder -Filter "${base_name}_p.dds"   -ErrorAction SilentlyContinue
        $s_map   = Get-ChildItem -LiteralPath $map_folder -Filter "${base_name}_s.dds"   -ErrorAction SilentlyContinue
        $cnr_map = Get-ChildItem -LiteralPath $map_folder -Filter "${base_name}_cnr.dds" -ErrorAction SilentlyContinue

        if ($relative_path) {
            $texture_value = "$relative_path\$json_name" -replace '\\','\\'
        } else {
            $texture_value = $json_name
        }

        if ($is_renamed) {
            $rename_value = "$relative_path\$base_name" -replace '\\','\\'
        }

        $emissive   = if ($g_map) { "true" } else { "false" }
        $parallax   = if ($p_map) { "true" } else { "false" }
        $multilayer = if ($cnr_map) { "true" } else { "false" }
        $subsurface = if ($s_map -and -not $cnr_map) { "true" } else { "false" }
        $disp_scale = if ($multilayer -eq "true") { $MultilayerDisplacementScale } else { $DisplacementScale }

        $props = @(
            "        `"texture`": `"$texture_value`","
        )

        if ($is_renamed) {
            $props += "        `"rename`": `"$rename_value`","
        }

        $props += @(
            "        `"emissive`": $emissive,",
            "        `"parallax`": $parallax,",
            "        `"subsurface_foliage`": false,",
            "        `"subsurface`": $subsurface,",
            "        `"specular_level`": $SpecularLevel,",
            "        `"subsurface_color`": [1, 1, 1],",
            "        `"roughness_scale`": $RoughnessScale,",
            "        `"subsurface_opacity`": $SubsurfaceOpacity,",
            "        `"smooth_angle`": false,",
            "        `"displacement_scale`": $disp_scale"
        )

        if ($multilayer -eq "true") {
            $props[-1] += ","
            $props += @(
                "        `"multilayer`": true,",
                "        `"coat_diffuse`": true,",
                "        `"coat_normal`": true,",
                "        `"coat_parallax`": true,",
                "        `"coat_strength`": $CoatStrength,",
                "        `"coat_roughness`": $CoatRoughness,",
                "        `"coat_specular_level`": $CoatSpecularLevel"
            )
        }

        if ($f_map) {
            $props[-1] += ","
            $props += "        `"fuzz`": { `"texture`": true }"
        }

        $json = @(
            "[",
            "    {",
            ($props -join "`n"),
            "    }",
            "]"
        ) -join "`n"

        $json | Set-Content -LiteralPath $file.FullName -Encoding UTF8

        if ($is_renamed -and $d_map) {
            $new_name = $d_map.Name -replace "_d\.dds$", ".dds"
            $new_path = Join-Path $d_map.DirectoryName $new_name
            if (-not (Test-Path -LiteralPath $new_path)) {
                Rename-Item -LiteralPath $d_map.FullName -NewName $new_name
            }
        }

        Write-Host "Processed $($file.Name)"
    }
    catch {
        Write-Host "[ERROR] $($file.Name): $($_.Exception.Message)"
    }
    $done++
    Write-Host "__SKYKING_PROGRESS__=$done/$total"
}

Write-Host "[DONE] Step 2 complete."
exit 0
