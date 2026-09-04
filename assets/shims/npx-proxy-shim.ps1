Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$realNpx = $env:ANTIGRAVITY_REAL_NPX
if ([string]::IsNullOrWhiteSpace($realNpx) -or -not (Test-Path -LiteralPath $realNpx -PathType Leaf)) {
    Write-Error 'ANTIGRAVITY_REAL_NPX does not point to a valid npx command.'
    exit 87
}

$forwardedArguments = @($args | ForEach-Object { [string]$_ })
$isChromeDevtools = @($forwardedArguments | Where-Object { $_ -match 'chrome-devtools-mcp' }).Count -gt 0

if ($isChromeDevtools) {
    $hasProxyServer = @($forwardedArguments | Where-Object { $_ -like '--proxy-server=*' }).Count -gt 0
    $hasProxyBypass = @($forwardedArguments | Where-Object { $_ -like '*--proxy-bypass-list=*' }).Count -gt 0

    if (-not $hasProxyServer -and -not [string]::IsNullOrWhiteSpace($env:ANTIGRAVITY_PROXY_URL)) {
        $forwardedArguments += "--proxy-server=$($env:ANTIGRAVITY_PROXY_URL)"
    }
    if (-not $hasProxyBypass -and -not [string]::IsNullOrWhiteSpace($env:ANTIGRAVITY_PROXY_BYPASS)) {
        $forwardedArguments += "--chrome-arg=--proxy-bypass-list=$($env:ANTIGRAVITY_PROXY_BYPASS)"
    }
}

try {
    & $realNpx @forwardedArguments
    exit $LASTEXITCODE
}
catch {
    Write-Error $_.Exception.Message
    exit 88
}
