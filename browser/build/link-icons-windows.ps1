# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 Bogdan Shapovalov and the Fury authors
#
# Put Fury's icons into the Chromium tree for a Windows build.
#
#     powershell -ExecutionPolicy Bypass -File core\build\link-icons-windows.ps1
#
# link-icons.sh does this for macOS and only for macOS: it writes app.icns and
# Assets.car, and on Windows nothing wrote anything. Every Windows core up to
# 0.2.3 therefore shipped Chromium's blue ball -- on the taskbar, in Alt-Tab, on
# every browser window -- beside an application called Fury. A tester asked for
# the branded icons on 27.09.2026.
#
# Generated here from assets\icon.png rather than committed, for the reason
# .gitignore gives for browser/branding: one source, so nobody ships two logos.
# PowerShell and System.Drawing because the build box has no Python, and a
# build step that needs a dependency installed first is one that gets skipped.
#
# What Windows reads, and so what is written:
#   chromium\win\chromium.ico   IDR_MAINFRAME in chrome_exe.rc and chrome_dll.rc:
#                               the .exe, the taskbar, every window
#   chromium\win\app_list.ico   the same mark, for the app launcher entry
#   chromium\win\tiles\*.png    Start menu tiles (VisualElementsManifest)
#   chromium\product_logo_*.png and default_{100,200}_percent\chromium\
#                               product_logo_{16,32}.png: logos drawn inside the
#                               browser -- window icon, About, profile menu
# Each file keeps the pixel size of the one it replaces, read from that file,
# so an upstream size change is followed rather than overwritten.

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

$root  = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$src   = Join-Path $root 'assets\icon.png'
$theme = Join-Path $root 'core\src\chrome\app\theme'
if (-not (Test-Path $src))   { throw "assets\icon.png is missing. See assets\README.md." }
if (-not (Test-Path $theme)) { throw "No Chromium tree at $theme. Run fetch.sh first." }

$master = [System.Drawing.Image]::FromFile($src)

function Render([int]$size) {
    $bmp = New-Object System.Drawing.Bitmap $size, $size, ([System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.InterpolationMode  = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.SmoothingMode      = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
    $g.PixelOffsetMode    = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $g.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
    $g.Clear([System.Drawing.Color]::Transparent)
    $g.DrawImage($master, 0, 0, $size, $size)
    $g.Dispose()
    return $bmp
}

function PngBytes([int]$size) {
    $bmp = Render $size
    $ms = New-Object System.IO.MemoryStream
    $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
    return ,$ms.ToArray()
}

# The PNG this replaces, redrawn at its own size.
function ReplacePng([string]$path) {
    if (-not (Test-Path $path)) { Write-Host "   -- absent, skipped: $path"; return }
    $old = [System.Drawing.Image]::FromFile($path)
    $w = $old.Width; $h = $old.Height
    $old.Dispose()
    if ($w -ne $h) { throw "$path is ${w}x${h}; expected a square" }
    [System.IO.File]::WriteAllBytes($path, (PngBytes $w))
    Write-Host "   $w px  $path"
}

# An .ico whose frames are PNGs -- the format Windows has read since Vista, and
# the one Chromium's own chromium.ico uses for its 256 px frame.
function WriteIco([string]$path, [int[]]$sizes) {
    $frames = @()
    foreach ($s in $sizes) { $frames += ,(PngBytes $s) }
    $ms = New-Object System.IO.MemoryStream
    $bw = New-Object System.IO.BinaryWriter $ms
    $bw.Write([UInt16]0); $bw.Write([UInt16]1); $bw.Write([UInt16]$sizes.Count)
    $offset = 6 + 16 * $sizes.Count
    for ($i = 0; $i -lt $sizes.Count; $i++) {
        $s = $sizes[$i]; $len = $frames[$i].Length
        $dim = if ($s -ge 256) { 0 } else { $s }   # 0 means 256 in an ICONDIRENTRY
        $bw.Write([byte]$dim); $bw.Write([byte]$dim)
        $bw.Write([byte]0); $bw.Write([byte]0)       # no palette, reserved
        $bw.Write([UInt16]1); $bw.Write([UInt16]32)  # planes, bits per pixel
        $bw.Write([UInt32]$len); $bw.Write([UInt32]$offset)
        $offset += $len
    }
    foreach ($f in $frames) { $bw.Write($f) }
    $bw.Flush()
    [System.IO.File]::WriteAllBytes($path, $ms.ToArray())
    $bw.Dispose()
    Write-Host "   ico $($sizes -join ',')  $path"
}

Write-Host "==> Windows icons from $src"
$win = Join-Path $theme 'chromium\win'
WriteIco (Join-Path $win 'chromium.ico') @(16, 20, 24, 32, 40, 48, 64, 256)
WriteIco (Join-Path $win 'app_list.ico') @(16, 24, 32, 48, 256)
ReplacePng (Join-Path $win 'tiles\Logo.png')
ReplacePng (Join-Path $win 'tiles\SmallLogo.png')

Write-Host "==> product logos"
foreach ($s in 16, 24, 48, 64, 128, 256) {
    ReplacePng (Join-Path $theme "chromium\product_logo_$s.png")
}
foreach ($scale in 'default_100_percent', 'default_200_percent') {
    foreach ($s in 16, 32) {
        ReplacePng (Join-Path $theme "$scale\chromium\product_logo_$s.png")
    }
}

$master.Dispose()
Write-Host "==> done"
