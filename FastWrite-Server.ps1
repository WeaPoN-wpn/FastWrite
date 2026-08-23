# FastWrite local server
# Serves the folder over http://localhost so Chrome/Edge remembers the
# microphone permission (file:// pages do not persist permissions).
# Usage: double-click FastWrite-Launcher.bat, or run:
#   powershell -NoProfile -ExecutionPolicy Bypass -File FastWrite-Server.ps1
param([switch]$NoOpen)

$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($root)) { $root = (Get-Location).Path }

function Find-HeaderEnd {
    param([byte[]]$data)
    for ($i = 0; $i -le $data.Length - 4; $i++) {
        if ($data[$i] -eq 13 -and $data[$i + 1] -eq 10 -and $data[$i + 2] -eq 13 -and $data[$i + 3] -eq 10) { return $i }
    }
    return -1
}

function Send-Response {
    param($stream, $status, $contentType, [byte[]]$body)
    $headerText = "HTTP/1.1 $status`r`nContent-Type: $contentType`r`nContent-Length: $($body.Length)`r`nCache-Control: no-store`r`nConnection: close`r`n`r`n"
    $headerBytes = [System.Text.Encoding]::ASCII.GetBytes($headerText)
    $stream.Write($headerBytes, 0, $headerBytes.Length)
    $stream.Write($body, 0, $body.Length)
    $stream.Flush()
}

try {
    $preferredPort = 8964
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $preferredPort)
        $listener.Start()
        $port = $preferredPort
    }
    catch {
        $probe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
        $probe.Start()
        $port = ($probe.LocalEndpoint).Port
        $probe.Stop()
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $port)
        $listener.Start()
    }

    $url = "http://localhost:$port/Handheld.html"

    Write-Host ""
    Write-Host "=============================================="
    Write-Host "  FastWrite is running"
    Write-Host "  $url"
    Write-Host "  Close this window to stop the server."
    Write-Host "=============================================="
    Write-Host ""

    if (-not $NoOpen) {
        Start-Process $url
    }

    $rootFull = [System.IO.Path]::GetFullPath($root)

    while ($true) {
        $client = $null
        try {
            $client = $listener.AcceptTcpClient()
            $client.NoDelay = $true
            $stream = $client.GetStream()
            # Chrome opens silent "preconnect" sockets; never block the single
            # accept loop on them - close idle connections quickly.
            $stream.ReadTimeout = 1500

            $buffer = New-Object byte[] 8192
            $ms = New-Object System.IO.MemoryStream
            while ((Find-HeaderEnd ($ms.ToArray())) -lt 0) {
                $n = $stream.Read($buffer, 0, $buffer.Length)
                if ($n -le 0) { break }
                $ms.Write($buffer, 0, $n)
                if ($ms.Length -gt 1048576) { break }
            }
            $raw = $ms.ToArray()
            $headerEnd = Find-HeaderEnd $raw
            if ($headerEnd -lt 0) { $client.Close(); continue }

            $headerBlock = [System.Text.Encoding]::ASCII.GetString($raw, 0, $headerEnd)
            $firstLine = ($headerBlock -split "`r`n")[0]
            $parts = $firstLine -split ' '
            if ($parts.Count -lt 2) { $client.Close(); continue }
            $method = $parts[0]
            $path = [System.Uri]::UnescapeDataString($parts[1])

            if ($method -ne 'GET') {
                Send-Response $stream '405 Method Not Allowed' 'text/plain; charset=utf-8' ([System.Text.Encoding]::UTF8.GetBytes('Method Not Allowed'))
                $client.Close(); continue
            }

            $rel = $path.TrimStart('/')
            if ($rel -eq '') { $rel = 'Handheld.html' }
            $full = [System.IO.Path]::GetFullPath((Join-Path $root $rel))
            if ($full.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $full -PathType Leaf)) {
                $fileBody = [System.IO.File]::ReadAllBytes($full)
                $ext = [System.IO.Path]::GetExtension($full).ToLowerInvariant()
                $contentType = 'application/octet-stream'
                switch ($ext) {
                    '.html' { $contentType = 'text/html; charset=utf-8' }
                    '.css'  { $contentType = 'text/css; charset=utf-8' }
                    '.js'   { $contentType = 'text/javascript; charset=utf-8' }
                    '.png'  { $contentType = 'image/png' }
                    '.jpg'  { $contentType = 'image/jpeg' }
                    '.svg'  { $contentType = 'image/svg+xml' }
                    '.ico'  { $contentType = 'image/x-icon' }
                }
                Send-Response $stream '200 OK' $contentType $fileBody
            }
            else {
                Send-Response $stream '404 Not Found' 'text/plain; charset=utf-8' ([System.Text.Encoding]::UTF8.GetBytes('Not Found'))
            }
            $client.Close()
        }
        catch {
            if ($client) { try { $client.Close() } catch { } }
        }
    }
}
catch {
    Write-Host ""
    Write-Host "ERROR: $_"
    Write-Host ""
    Read-Host "Press Enter to close this window"
}
