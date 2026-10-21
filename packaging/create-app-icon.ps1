$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

function New-RoundedPath([single]$size, [single]$radius) {
    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $d = 2 * $radius
    $path.AddArc(0, 0, $d, $d, 180, 90)
    $path.AddArc($size - $d, 0, $d, $d, 270, 90)
    $path.AddArc($size - $d, $size - $d, $d, $d, 0, 90)
    $path.AddArc(0, $size - $d, $d, $d, 90, 90)
    $path.CloseFigure()
    return $path
}

function New-LogoPng([int]$size) {
    $bitmap = New-Object System.Drawing.Bitmap($size, $size, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $graphics.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
    $scale = [single]$size / 256
    $graphics.ScaleTransform($scale, $scale)

    $background = New-RoundedPath 256 62
    $brush = New-Object System.Drawing.Drawing2D.LinearGradientBrush(
        (New-Object System.Drawing.PointF(24,18)), (New-Object System.Drawing.PointF(230,240)),
        ([System.Drawing.ColorTranslator]::FromHtml('#dcecff')),
        ([System.Drawing.ColorTranslator]::FromHtml('#9fc5ec')))
    $graphics.FillPath($brush, $background)
    $brush.Dispose(); $background.Dispose()

    $barColor = [System.Drawing.Color]::FromArgb(97, 79, 133, 189)
    $bars = @(@(52,188,52,158), @(86,188,86,130), @(120,188,120,147), @(154,188,154,100))
    foreach ($bar in $bars) {
        $pen = New-Object System.Drawing.Pen($barColor, 18)
        $pen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
        $pen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
        $graphics.DrawLine($pen, $bar[0], $bar[1], $bar[2], $bar[3])
        $pen.Dispose()
    }

    $red = [System.Drawing.ColorTranslator]::FromHtml('#ff646b')
    $pen = New-Object System.Drawing.Pen($red, 20)
    $pen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
    $pen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
    $pen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
    $trend = New-Object System.Drawing.Drawing2D.GraphicsPath
    $trend.AddLines([System.Drawing.Point[]]@(
        (New-Object System.Drawing.Point(50,162)),
        (New-Object System.Drawing.Point(92,126)),
        (New-Object System.Drawing.Point(123,143)),
        (New-Object System.Drawing.Point(190,73))))
    $graphics.DrawPath($pen, $trend)
    $graphics.DrawLine($pen, 158, 73, 190, 73)
    $graphics.DrawLine($pen, 190, 73, 190, 105)
    $trend.Dispose(); $pen.Dispose()

    $stream = New-Object System.IO.MemoryStream
    $bitmap.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
    $bytes = $stream.ToArray()
    $stream.Dispose(); $graphics.Dispose(); $bitmap.Dispose()
    return ,$bytes
}

$sizes = @(16, 24, 32, 48, 64, 128, 256)
$frames = @($sizes | ForEach-Object { [pscustomobject]@{ Size = $_; Bytes = (New-LogoPng $_) } })
$path = Join-Path $PSScriptRoot 'app_icon.ico'
$file = [System.IO.File]::Create($path)
$writer = New-Object System.IO.BinaryWriter($file)
$writer.Write([UInt16]0); $writer.Write([UInt16]1); $writer.Write([UInt16]$frames.Count)
$offset = 6 + 16 * $frames.Count
foreach ($frame in $frames) {
    $dimension = if ($frame.Size -eq 256) { [byte]0 } else { [byte]$frame.Size }
    $writer.Write($dimension); $writer.Write($dimension); $writer.Write([byte]0); $writer.Write([byte]0)
    $writer.Write([UInt16]1); $writer.Write([UInt16]32)
    $writer.Write([UInt32]$frame.Bytes.Length); $writer.Write([UInt32]$offset)
    $offset += $frame.Bytes.Length
}
foreach ($frame in $frames) { $writer.Write([byte[]]$frame.Bytes) }
$writer.Dispose(); $file.Dispose()
Write-Output "Created $path"
