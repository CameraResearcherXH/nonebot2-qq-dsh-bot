<#
  find_napcat_mirror.ps1  --  probe Docker mirrors for the NapCat image
  (Windows counterpart of find_napcat_mirror.sh; called by find_napcat_mirror.bat)

  Background (2026-11-28):
    mlikiowa/napcat-docker was removed from Docker Hub and NapNeko/NapCat-Docker
    is 404. Mirrors only keep tags they once cached, so "latest" is often gone.

  Why not "docker manifest inspect":
    It has NO timeout, and the CLI tries auth.docker.io which is unreachable on
    this machine (curl rc=28) -> it hangs for minutes. That was the "stuck" bug.

  Why not plain "curl -f":
    These registries answer 401 + WWW-Authenticate first, so a bare curl turns
    "image exists" into a false negative. We do 401 -> token -> manifest.

  Usage:
    find_napcat_mirror.bat
    find_napcat_mirror.bat --write-env     (write NAPCAT_IMAGE into ..\.env)
    find_napcat_mirror.bat --pull          (pull + local tag + save tar)

  Env overrides:  set IMAGE=...  |  set TAGS=...  |  set MIRRORS=...
#>

$ErrorActionPreference = 'Stop'

$doPull = $false
$doEnv  = $false
foreach ($a in $args) {
    switch ($a.ToLower()) {
        '--pull'      { $doPull = $true }
        '--write-env' { $doEnv  = $true }
        '-h'          { Get-Help $PSCommandPath -Detailed; exit 0 }
        '--help'      { Get-Help $PSCommandPath -Detailed; exit 0 }
        default       { Write-Host "[!] unknown arg: $a  (use --pull / --write-env)"; exit 1 }
    }
}

$image = if ($env:IMAGE) { $env:IMAGE } else { 'mlikiowa/napcat-docker' }
$tags  = if ($env:TAGS)  { $env:TAGS }  else { 'latest v4.19.0 v4.18.3 v4.18.0 v4.17.45 v4.17.0 v4.16.1 v4.15.0' }
# daocloud first: as of 2026-11-28 it is the only mirror that still caches it
$mirrors = if ($env:MIRRORS) { $env:MIRRORS } else { 'docker.m.daocloud.io docker.1ms.run docker.xuanyuan.me dockerproxy.net docker.1panel.live hub.rat.dev' }

$accept = 'application/vnd.docker.distribution.manifest.v2+json,application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.oci.image.manifest.v1+json'

if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) {
    Write-Host '[X] curl.exe not found (Windows 10 1803+ ships it in C:\Windows\System32)'
    exit 1
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host '[X] docker not found in PATH'
    exit 1
}

# returns: 200 = found | 404 = tag missing (mirror alive) | 0/5xx = unreachable | 403 = refused
function Get-ManifestCode([string]$mirror, [string]$tag) {
    $url = "https://$mirror/v2/$image/manifests/$tag"
    $hdr = & curl.exe -s -L -m 6 -D - -o NUL -H "Accept: $accept" $url 2>$null
    if ($LASTEXITCODE -ne 0) { return 0 }

    $status = 0
    foreach ($l in @($hdr)) {
        if ($l -match '^HTTP/\S+\s+(\d{3})') { $status = [int]$Matches[1] }
    }
    if ($status -ne 401) { return $status }

    $authLine = $null
    foreach ($l in @($hdr)) {
        if ($l -match '(?i)^www-authenticate:\s*(.+)$') { $authLine = $Matches[1] }
    }
    if (-not $authLine) { return 403 }

    $realm = if ($authLine -match 'realm="([^"]+)"')   { $Matches[1] } else { $null }
    $svc   = if ($authLine -match 'service="([^"]+)"') { $Matches[1] } else { '' }
    $scp   = if ($authLine -match 'scope="([^"]+)"')   { $Matches[1] } else { '' }
    if (-not $realm) { return 403 }
    # some mirrors (e.g. docker.m.daocloud.io) omit scope in the 401 challenge;
    # without it the token request fails and we would misreport "unreachable".
    if (-not $scp) { $scp = "repository:$image:pull" }
    $sep = if ($realm.Contains('?')) { '&' } else { '?' }
    $tokUrl = "$realm${sep}service=$svc&scope=$scp"

    $tokJson = & curl.exe -s -m 6 $tokUrl 2>$null
    $tok = $null
    if ($tokJson -match '"token"\s*:\s*"([^"]+)"')             { $tok = $Matches[1] }
    elseif ($tokJson -match '"access_token"\s*:\s*"([^"]+)"')  { $tok = $Matches[1] }
    if (-not $tok) { return 403 }

    $code = & curl.exe -s -m 8 -o NUL -w "%{http_code}" -H "Authorization: Bearer $tok" -H "Accept: $accept" $url 2>$null
    $n = 0
    if ([int]::TryParse(("$code").Trim(), [ref]$n)) { return $n }
    return 0
}

Write-Host "Image   : $image"
Write-Host "Mirrors : $mirrors"
Write-Host "Tags    : $tags"
Write-Host ('-' * 70)

$found = @()
foreach ($m in $mirrors.Split(' ', [System.StringSplitOptions]::RemoveEmptyEntries)) {
    foreach ($t in $tags.Split(' ', [System.StringSplitOptions]::RemoveEmptyEntries)) {
        $c = Get-ManifestCode $m $t
        if ($c -eq 200) {
            Write-Host "[OK] $m/$image`:$t"
            $found += "$m/$image`:$t"
            break                       # first working tag on this mirror is enough
        }
        if ($c -eq 404 -or $c -eq 401) {
            Write-Host "[--] $m/$image`:$t"
            continue                    # mirror alive, this tag is missing
        }
        Write-Host "[!!] $m unreachable/refused (code $c) - skip its remaining tags"
        break
    }
}

Write-Host ('-' * 70)
if ($found.Count -eq 0) {
    Write-Host '[X] no mirror had the image. Three options:'
    Write-Host '    1) widen the tag list:  set TAGS=v4.18.3 v4.17.45  and run again'
    Write-Host '       (find your current version:  docker images | findstr napcat)'
    Write-Host '    2) docker save the image on a machine that still has it, docker load here'
    Write-Host '    3) switch protocol backend:  lagrangedev/lagrange.core'
    exit 2
}

Write-Host 'Available:'
foreach ($f in $found) { Write-Host "  NAPCAT_IMAGE=$f" }
$pick = $found[0]
Write-Host ''
Write-Host "Picked: $pick"

if ($doPull) {
    $tag = ($pick -split ':')[-1]
    Write-Host "== pull $pick =="
    docker pull $pick
    if ($LASTEXITCODE -ne 0) { exit 3 }
    Write-Host "== local tag napcat-local:$tag =="
    docker tag $pick "napcat-local:$tag"
    $tar = Join-Path $env:USERPROFILE 'napcat-image.tar'
    Write-Host "== save to $tar =="
    docker save -o $tar "napcat-local:$tag"
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[OK] saved: $tar  (load elsewhere: docker load -i)"
    } else {
        Write-Host '[!] docker save failed'
    }
    Write-Host ''
    Write-Host "For this machine .env: NAPCAT_IMAGE=napcat-local:$tag  (decoupled from upstream)"
    $pick = "napcat-local:$tag"
}

if ($doEnv) {
    $root  = Split-Path -Parent $PSScriptRoot
    $envf  = Join-Path $root '.env'
    if (-not (Test-Path -LiteralPath $envf)) {
        Write-Host "[X] .env not found: $envf"
        Write-Host '    run:  copy .env.example .env'
        exit 4
    }
    Copy-Item -LiteralPath $envf -Destination "$envf.bak" -Force
    $lines = @(Get-Content -LiteralPath $envf | Where-Object { $_ -notmatch '^\s*NAPCAT_IMAGE=' })
    $lines += "NAPCAT_IMAGE=$pick"
    # write UTF-8 WITHOUT BOM: a BOM would break the first key in .env
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($envf, $lines, $utf8)
    Write-Host "[OK] wrote $envf  NAPCAT_IMAGE=$pick   (backup: .env.bak)"
}

Write-Host ''
Write-Host 'Done. Apply with:  docker compose up -d --force-recreate napcat'
exit 0
