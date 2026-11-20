# ============================================================
# LoRA one-click download (Civitai API) for the draw pipeline
#
# IMPORTANT:
#   - Civitai.com is blocked from CN networks. You MUST either:
#       a) run this with a proxy (-Proxy "http://127.0.0.1:7890"), or
#       b) run it on a machine that can reach civitai.com (e.g. your HK server),
#          then copy the .safetensors files into H:\ComfyUI\models\loras\.
#   - Get a Civitai API token at: civitai.com -> Settings -> API Keys.
#     Many LoRA files require it. Pass it with -Token.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File setup_loras.ps1 -Token "YOUR_TOKEN" -Proxy "http://127.0.0.1:7890"
#   (omit -Proxy if the machine can already reach civitai.com)
#   add -DryRun to only print what would be downloaded.
#
# It resolves each LoRA by name via the Civitai search API, filters to
# SDXL/Illustrious/Pony base models, then downloads the first matching version.
# ============================================================
param(
    [string]$LoraDir = "H:\ComfyUI\models\loras",
    [string]$Token = "",
    [string]$Proxy = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# List of (SaveAsFilename, SearchName, KnownModelId). KnownModelId 0 = resolve by search.
$LORAS = @(
    @{ Save = "AddMicroDetails_NoobAI.safetensors";  Query = "AddMicroDetails NoobAI";   Id = 0 },
    @{ Save = "DetailTweakerXL.safetensors";         Query = "Detail Tweaker XL";        Id = 0 },
    @{ Save = "ThickerLinesAnimeStyle.safetensors";  Query = "Thicker Lines Anime Style"; Id = 0 },
    @{ Save = "FlatColor_Illustrious.safetensors";   Query = "Flat Color Illustrious";   Id = 0 },
    @{ Save = "Nnegrat_2_5D_High.safetensors";       Query = "Nnegrat 2.5D High";        Id = 0 },
    @{ Save = "epi_noiseoffset.safetensors";         Query = "epi_noiseoffset";          Id = 13941 }
)

$API = "https://civitai.com/api/v1"
$headers = @{}
if ($Token) {
    $headers["Authorization"] = "Bearer $Token"
}

function Get-Json($url) {
    if ($Proxy) {
        return Invoke-RestMethod -Uri $url -Headers $headers -Proxy $Proxy -TimeoutSec 30
    }
    return Invoke-RestMethod -Uri $url -Headers $headers -TimeoutSec 30
}

function Resolve-Model($entry) {
    if ($entry.Id -gt 0) {
        return Get-Json "$API/models/$($entry.Id)"
    }
    $q = [uri]::EscapeDataString($entry.Query)
    $r = Get-Json "$API/models?query=$q&types=LORA&limit=20"
    $items = @($r.items)
    if (-not $items.Count) { return $null }
    # prefer Illustrious/SDXL/Pony base models (NoobAI/Animagine are SDXL-based)
    $pick = $items | Where-Object { $_.type -eq "LORA" -and $_.baseModel -match "SDXL|Illustrious|Pony" } | Select-Object -First 1
    if (-not $pick) { $pick = $items | Select-Object -First 1 }
    return $pick
}

function Get-DownloadInfo($model) {
    $ver = @($model.modelVersions) | Where-Object { $_.files } | Select-Object -First 1
    if (-not $ver) { return $null }
    # prefer .safetensors (ComfyUI needs it, skip .pt)
    $st = @($ver.files) | Where-Object { $_.name -match '\.safetensors$' }
    $pool = if ($st) { $st } else { @($ver.files) }
    $file = $pool | Where-Object { $_.type -match "Model|Pruned|Trained" } | Select-Object -First 1
    if (-not $file) { $file = @($pool)[0] }
    return @{ versionId = $ver.id; url = $file.downloadUrl; name = $file.name }
}

New-Item -ItemType Directory -Force -Path $LoraDir | Out-Null

foreach ($e in $LORAS) {
    Write-Host ""
    Write-Host "== $($e.Query) -> $($e.Save)" -ForegroundColor Cyan
    try {
        $model = Resolve-Model $e
        if (-not $model) {
            Write-Host "  [WARN] not found on Civitai" -ForegroundColor Yellow
            continue
        }
        $info = Get-DownloadInfo $model
        if (-not $info) {
            Write-Host "  [WARN] no downloadable file" -ForegroundColor Yellow
            continue
        }
        Write-Host ("  model: {0} (base {1})" -f $model.name, $model.baseModel)
        Write-Host ("  file : {0}" -f $info.name)
        $out = Join-Path $LoraDir $e.Save
        if ($DryRun) {
            Write-Host "  [DRY] would download to $out" -ForegroundColor DarkYellow
            continue
        }
        if (Test-Path $out) {
            Write-Host "  [SKIP] already exists: $out"
            continue
        }
        # use the JSON downloadUrl (contains required fileId); fallback to API endpoint
        if ($info.url) { $dl = $info.url } else { $dl = "$API/download/models/$($info.versionId)" }
        if ($Proxy) {
            Invoke-WebRequest -Uri $dl -Headers $headers -OutFile $out -Proxy $Proxy -MaximumRedirection 10
        } else {
            Invoke-WebRequest -Uri $dl -Headers $headers -OutFile $out -MaximumRedirection 10
        }
        $kb = [math]::Round((Get-Item $out).Length / 1KB)
        Write-Host "  [OK] saved $out ($kb KB)" -ForegroundColor Green
    } catch {
        Write-Host "  [FAIL] $($_.Exception.Message)" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "done. Reminder:"
Write-Host "  - filenames must match SD_LORAS / SD_LORA_TRIGGERS in node.env"
Write-Host "  - trigger words: addmicrodetails / add detail / thicker lines / flat color"
Write-Host "  - epi_noiseoffset trigger words: dark studio, rim lighting, two tone lighting, dimly lit, low key"
