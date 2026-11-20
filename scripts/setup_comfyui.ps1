# ============================================================
# ComfyUI one-click setup for compute node (Windows + 3090Ti, CN network)
#
# Run in an ADMIN PowerShell:
#   powershell -ExecutionPolicy Bypass -File E:\deepseek\setup_comfyui.ps1
#
# Params:
#   $PythonExe     python.exe to use (must be 3.10~3.12). Default: the 3.12.9
#                  install found on this machine. Change it if your path differs.
#   $ComfyDir      install dir (default H:\ComfyUI, NO Chinese chars in path)
#   $CacheDir      where all runtime caches go (pip/torch/huggingface), default H:\cache
#   $SkipAnimagine skip the Animagine XL 4.0 backup checkpoint
#   $PipMirror     pip index mirror (default Tsinghua)
#   $TorchIndex    torch wheel source, passed via --find-links (default aliyun cu126
#                  flat listing). ComfyUI needs torch>=2.7, so cu121/cu124 are too
#                  old; cu128 needs driver>=570.)
#
# What it does (everything needed today, in one go):
#   1) check python (must be 3.10~3.12) / git / nvidia-smi + H: drive
#   2) git clone ComfyUI + create venv (all on H:)
#   3) install PyTorch cu121 (CN mirror)
#   4) install ComfyUI deps + 3 custom nodes (Manager / controlnet_aux / IPAdapter_plus)
#   5) download all models from hf-mirror:
#        NoobAI-XL eps (main) + Animagine XL 4.0 (backup)
#        sdxl_vae (spare), 4x-UltraSharp (HiRes)
#        xinsir controlnet-union promax (lineart/pose/depth in one file)
#        IP-Adapter Plus SDXL + CLIP-ViT-H14 (style reference)
# ============================================================
param(
    [string]$PythonExe = "C:\Users\admin\AppData\Local\Programs\Python\Python312\python.exe",
    [string]$ComfyDir = "H:\ComfyUI",
    [string]$CacheDir = "H:\cache",
    [switch]$SkipAnimagine,
    [string]$PipMirror = "https://pypi.tuna.tsinghua.edu.cn/simple",
    [string]$TorchIndex = "https://mirrors.aliyun.com/pytorch-wheels/cu126/"
)

$ErrorActionPreference = "Stop"
$env:HF_ENDPOINT = "https://hf-mirror.com"

# ---------- everything goes to H: (keep C: clean) ----------
if (-not (Test-Path "H:\")) {
    Write-Host "[ERROR] H: drive not found. Plug it in or change -ComfyDir / -CacheDir." -ForegroundColor Red
    exit 1
}
New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
$env:HF_HOME = "$CacheDir\huggingface"
$env:HUGGINGFACE_HUB_CACHE = "$CacheDir\huggingface\hub"
$env:PIP_CACHE_DIR = "$CacheDir\pip"
$env:TORCH_HOME = "$CacheDir\torch"
$env:XDG_CACHE_HOME = "$CacheDir\xdg"

function Step($msg) {
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host "  $msg" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
}

function Fail($msg) {
    Write-Host ""
    Write-Host "[ERROR] $msg" -ForegroundColor Red
    exit 1
}

function Check($name, $cmd) {
    Write-Host -NoNewline "check $name ... "
    try {
        $null = Invoke-Expression $cmd 2>$null
        Write-Host "OK" -ForegroundColor Green
        return $true
    } catch {
        Write-Host "MISSING" -ForegroundColor Red
        return $false
    }
}

# ---------- 0. preflight ----------
Step "0/6 preflight"
# tolerate missing .exe suffix (command line auto-appends it, Test-Path does not)
if (-not (Test-Path $PythonExe) -and -not $PythonExe.EndsWith(".exe")) {
    if (Test-Path "$PythonExe.exe") { $PythonExe = "$PythonExe.exe" }
}
if (-not (Test-Path $PythonExe)) {
    Fail "python.exe not found at: $PythonExe . Pass the right path via -PythonExe."
}
Write-Host -NoNewline "check python ... "
& $PythonExe --version
if ($LASTEXITCODE -ne 0) { Fail "cannot run $PythonExe" }
$pyver = & $PythonExe -c "import sys; print(str(sys.version_info.major)+'.'+str(sys.version_info.minor))"
Write-Host "python version = $pyver"
if ($pyver -notin @("3.10", "3.11", "3.12")) {
    Fail "Python $pyver is NOT supported by torch cu121. Use Python 3.10~3.12."
}
if (-not (Check "git" "git --version")) {
    Fail "git not found. Install from https://git-scm.com/download then rerun."
}
if (-not (Check "nvidia-smi" "nvidia-smi")) {
    Fail "nvidia-smi failed. Install the latest NVIDIA driver first."
}
nvidia-smi

# ---------- 1. clone ComfyUI ----------
Step "1/6 clone ComfyUI to $ComfyDir"
if (-not (Test-Path "$ComfyDir\.git")) {
    $parent = Split-Path $ComfyDir -Parent
    if (-not (Test-Path $parent)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    git clone https://github.com/comfyanonymous/ComfyUI.git $ComfyDir
    if ($LASTEXITCODE -ne 0) {
        Fail "git clone ComfyUI failed (GitHub unreachable?). Try a proxy, or clone from a gitee mirror manually into $ComfyDir then rerun."
    }
}
if (-not (Test-Path "$ComfyDir\requirements.txt")) {
    Fail "clone looks incomplete: $ComfyDir\requirements.txt missing. Delete $ComfyDir and rerun."
}
Set-Location $ComfyDir
Write-Host "ComfyUI source OK: $ComfyDir"

# ---------- 2. venv + PyTorch ----------
Step "2/6 venv + PyTorch cu121"
# reuse venv only if it was built with the SAME python version; otherwise rebuild
if (Test-Path "$ComfyDir\venv\Scripts\python.exe") {
    $venvPy = & "$ComfyDir\venv\Scripts\python.exe" -c "import sys; print(str(sys.version_info.major)+'.'+str(sys.version_info.minor))"
    if ($venvPy -ne $pyver) {
        Write-Host "[WARN] venv was built with Python $venvPy, current is $pyver. Rebuilding venv..." -ForegroundColor Yellow
        Remove-Item -Recurse -Force "$ComfyDir\venv"
    }
}
if (-not (Test-Path "$ComfyDir\venv")) {
    & $PythonExe -m venv venv
    if ($LASTEXITCODE -ne 0) { Fail "python -m venv failed." }
}
$py = "$ComfyDir\venv\Scripts\python.exe"
$pip = "$ComfyDir\venv\Scripts\pip.exe"
& $py -m pip install --upgrade pip
# CN network resets download.pytorch.org, so use aliyun pytorch wheels.
# NOTE: aliyun pytorch-wheels is a FLAT directory listing, not a PEP503 simple
#       index, so it must be passed via --find-links (pip parses the HTML links),
#       while --index-url points to a normal pypi mirror for the rest.
# NOTE 2: current ComfyUI (comfy-kitchen) needs torch>=2.7 (list[int] custom op),
#       and cu121 only goes up to 2.5.1 -> must use cu126.
#       Driver 560.94 supports CUDA 12.6 (cu126); cu128 would need driver>=570.
$torchOk = $false
foreach ($idx in @($TorchIndex)) {
    Write-Host "installing torch from $idx (via --find-links, pinned CUDA versions)"
    # Pin the exact +cu126 versions so pip cannot fall back to the CPU-only
    # torch wheel on PyPI. torchaudio is dropped: ComfyUI core does not need it,
    # and cu126 only ships torchaudio up to 2.11 (would clash with torch 2.14).
    & $pip install "torch==2.14.1+cu126" "torchvision==0.29.1+cu126" --find-links $idx --index-url $PipMirror --force-reinstall
    if ($LASTEXITCODE -eq 0) { $torchOk = $true; break }
}
if (-not $torchOk) {
    Fail "torch install failed. If 'from versions: none', no wheel matches your Python version (need 3.10~3.12)."
}
Write-Host "verify torch ..."
& $py -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
if ($LASTEXITCODE -ne 0) { Fail "torch import failed, check the error above." }

# ---------- 3. ComfyUI deps ----------
Step "3/6 install ComfyUI requirements (Tsinghua mirror)"
# ComfyUI recently added 'comfyui-workflow-templates' to requirements.txt:
# it requires Python <3.12 and pulls hundreds of MB of media wheels that often
# 404 on CN mirrors. We drive ComfyUI via API with our own workflows, so skip it.
$reqTmp = "$ComfyDir\requirements_no_templates.txt"
Get-Content "$ComfyDir\requirements.txt" |
    Where-Object { $_ -notmatch 'comfyui-workflow-templates' -and $_.Trim() -ne '' } |
    Set-Content $reqTmp
& $pip install -r $reqTmp -i $PipMirror
if ($LASTEXITCODE -ne 0) { Fail "ComfyUI requirements install failed." }

# ---------- 4. custom nodes ----------
Step "4/6 custom nodes (Manager / controlnet_aux / IPAdapter_plus)"
$CN = "$ComfyDir\custom_nodes"
New-Item -ItemType Directory -Force -Path $CN | Out-Null
if (-not (Test-Path "$CN\ComfyUI-Manager")) {
    git clone https://github.com/ltdrdata/ComfyUI-Manager.git "$CN\ComfyUI-Manager"
}
if (-not (Test-Path "$CN\comfyui_controlnet_aux")) {
    git clone https://github.com/Fannovel16/comfyui_controlnet_aux.git "$CN\comfyui_controlnet_aux"
}
if (-not (Test-Path "$CN\ComfyUI_IPAdapter_plus")) {
    git clone https://github.com/cubiq/ComfyUI_IPAdapter_plus.git "$CN\ComfyUI_IPAdapter_plus"
}
# node-specific deps (controlnet_aux can be flaky on Windows; if it fails, install via Manager UI)
try {
    if (Test-Path "$CN\comfyui_controlnet_aux\requirements.txt") {
        & $pip install -r "$CN\comfyui_controlnet_aux\requirements.txt" -i $PipMirror
    }
    if (Test-Path "$CN\ComfyUI_IPAdapter_plus\requirements.txt") {
        & $pip install -r "$CN\ComfyUI_IPAdapter_plus\requirements.txt" -i $PipMirror
    }
} catch {
    Write-Host "[WARN] some node deps failed to install. Fix via ComfyUI Manager UI later." -ForegroundColor Yellow
}

# ---------- 5. download models ----------
Step "5/6 download models from hf-mirror (CN fast)"
# huggingface-hub 2.x removed the huggingface-cli.exe entry point, so call the
# Python API directly instead of the CLI.
& $py -c "import huggingface_hub" 2>$null
if ($LASTEXITCODE -ne 0) {
    & $pip install huggingface-hub -i $PipMirror
    if ($LASTEXITCODE -ne 0) { Fail "huggingface-hub install failed." }
}

function HfGet($repo, $file, $dest) {
    Write-Host "downloading $repo / $file  ->  $dest"
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    & $py -c "from huggingface_hub import hf_hub_download; print(hf_hub_download(repo_id='$repo', filename='$file', local_dir=r'$dest'))"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[WARN] download failed: $repo / $file (check the filename on the repo page and retry manually)" -ForegroundColor Yellow
    }
}

# main checkpoint: NoobAI-XL v1.1 (single-file, official name on the repo)
HfGet "Laxhar/noobai-XL-1.1" "NoobAI-XL-v1.1.safetensors" "$ComfyDir\models\checkpoints"
# HiRes upscaler
HfGet "Kim2091/UltraSharp" "4x-UltraSharp.pth" "$ComfyDir\models\upscale_models"
# ControlNet union: one model for lineart / pose / depth (pair with comfyui_controlnet_aux)
HfGet "xinsir/controlnet-union-sdxl-1.0" "diffusion_pytorch_model_promax.safetensors" "$ComfyDir\models\controlnet"
# IP-Adapter Plus SDXL + CLIP vision
HfGet "h94/IP-Adapter" "sdxl_models/ip-adapter-plus_sdxl_vit-h.safetensors" "$ComfyDir\models\ipadapter"
HfGet "h94/IP-Adapter" "models/image_encoder/model.safetensors" "$ComfyDir\models\clip_vision"
# IP-Adapter CLIP vision must be named clip-vit-h14.safetensors; hf-cli keeps subdirs, move it
$clip = Get-ChildItem "$ComfyDir\models\clip_vision" -Recurse -Filter "model.safetensors" | Select-Object -First 1
if ($clip) {
    Move-Item $clip.FullName "$ComfyDir\models\clip_vision\clip-vit-h14.safetensors" -Force
}

# backup checkpoint (downloaded by default; add -SkipAnimagine to skip)
if (-not $SkipAnimagine) {
    HfGet "cagliostrolab/animagine-xl-4.0" "animagine-xl-4.0.safetensors" "$ComfyDir\models\checkpoints"
}
# spare VAE: NoobAI/Animagine already bake VAE, so this is rarely needed.
# Only switch to it in the workflow if output looks washed out / grey. 335MB, harmless to keep.
HfGet "stabilityai/sdxl-vae" "sdxl_vae.safetensors" "$ComfyDir\models\vae"

# ================= character LoRA (on demand) =================
# A LoRA does NOT work just by downloading: each one has trigger words that must
# appear in the prompt, and the server side needs a "character -> lora + triggers"
# mapping table. So do NOT download any by default. After launch, watch what the
# group actually requests, then add 2-3 characters properly.
#
# To add manually, uncomment and edit (repo = the LoRA's HF repo):
# HfGet "REPO_NAME" "charA.safetensors" "$ComfyDir\models\loras"
# HfGet "REPO_NAME" "charB.safetensors" "$ComfyDir\models\loras"
#
# NOTE: Civitai is usually unreachable from CN; prefer liblib.art / ModelScope,
# or an HF mirror repo if one exists.

# ---------- 6. launch ----------
Step "6/6 done. launch ComfyUI"
Write-Host ""
Write-Host "launch (bind 127.0.0.1 only, do NOT expose to LAN/internet):" -ForegroundColor Green
Write-Host "  cd $ComfyDir"
Write-Host "  .\venv\Scripts\activate"
Write-Host "  python main.py --listen 127.0.0.1 --port 8188 --preview-method auto"
Write-Host ""
Write-Host "open http://127.0.0.1:8188 and pick NoobAI-XL-v1.1 to test one image."
Write-Host ""
Write-Host "auto-start / crash-restart with NSSM (optional):" -ForegroundColor Yellow
Write-Host "  choco install nssm -y"
Write-Host "  nssm install ComfyUI `"$ComfyDir\venv\Scripts\python.exe`" main.py --listen 127.0.0.1 --port 8188"
Write-Host "  nssm set ComfyUI AppDirectory $ComfyDir"
Write-Host "  nssm start ComfyUI"
Write-Host ""
Write-Host "REMINDER: put node.py / node.env / comfy_workflows next to each other on this machine."
