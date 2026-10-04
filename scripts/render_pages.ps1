param(
    [Parameter(Mandatory = $true)][string]$Spec,
    [Parameter(Mandatory = $true)][string]$OutDir
)

# Draws every page with "mode": "image" in the spec to a JPEG, so the sample letters look like
# scans or photos instead of text PDFs. Needs Windows (GDI+ and the Nirmala UI font for Hindi).
Add-Type -AssemblyName System.Drawing
$config = Get-Content -Raw -Encoding UTF8 $Spec | ConvertFrom-Json
New-Item -ItemType Directory -Force $OutDir | Out-Null

$jpeg = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object { $_.MimeType -eq 'image/jpeg' }
$params = New-Object System.Drawing.Imaging.EncoderParameters 1
$params.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter ([System.Drawing.Imaging.Encoder]::Quality), ([long]82)
$random = New-Object System.Random 2026

foreach ($doc in $config.documents) {
    $index = 0
    foreach ($page in $doc.pages) {
        $index++
        if ($page.mode -ne 'image') { continue }

        $width = 1240; $height = 1754          # A4 at 150 dpi
        $bitmap = New-Object System.Drawing.Bitmap $width, $height
        $g = [System.Drawing.Graphics]::FromImage($bitmap)
        $g.SmoothingMode = 'AntiAlias'
        $g.TextRenderingHint = 'AntiAlias'
        $background = if ($page.background) { $page.background } else { '#FFFFFF' }
        $g.Clear([System.Drawing.ColorTranslator]::FromHtml($background))

        $g.TranslateTransform($width / 2, $height / 2)
        $g.RotateTransform([single]$page.rotate)
        $g.TranslateTransform(-$width / 2, -$height / 2)

        $font = New-Object System.Drawing.Font $page.font, ([single]$page.size), ([System.Drawing.FontStyle]::Regular), ([System.Drawing.GraphicsUnit]::Pixel)
        $brush = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255, 25, 25, 35))
        $y = [single]$page.top
        foreach ($line in $page.lines) {
            if ($line -is [string]) { $text = $line; $x = [single]$page.left }
            else { $text = $line.t; $x = [single]$line.x }
            if ($text) { $g.DrawString($text, $font, $brush, $x, $y) }
            $y += [single]$page.lineHeight
        }

        $g.ResetTransform()
        $speck = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(40, 90, 90, 90))
        for ($n = 0; $n -lt 1800; $n++) {
            $g.FillEllipse($speck, $random.Next(0, $width), $random.Next(0, $height), $random.Next(1, 4), $random.Next(1, 4))
        }

        $target = Join-Path $OutDir ("{0}-{1}.jpg" -f $doc.name, $index)
        $bitmap.Save($target, $jpeg, $params)
        $g.Dispose(); $bitmap.Dispose()
    }
}
