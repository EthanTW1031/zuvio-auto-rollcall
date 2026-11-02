$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::InputEncoding = [System.Text.Encoding]::UTF8

# Launcher for the window version (zuvio_gui.py).
# Finds Python, installs missing packages if needed, then opens the window
# with pythonw so no console window stays open.

function Write-Utf8Base64 {
    param([string]$Text)
    Write-Host ([System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($Text)))
}

function Find-Python {
    $candidates = @(
        @{ Command = "python"; Args = @("--version") },
        @{ Command = "py"; Args = @("-3", "--version") },
        @{ Command = "python3"; Args = @("--version") }
    )

    foreach ($candidate in $candidates) {
        try {
            $null = & $candidate.Command @($candidate.Args) 2>$null
            if ($LASTEXITCODE -eq 0) {
                return $candidate
            }
        } catch {
        }
    }

    return $null
}

function Run-Python {
    param(
        [hashtable]$Python,
        [string[]]$PythonArgs
    )

    if ($Python.Command -eq "py") {
        & $Python.Command -3 @PythonArgs
    } else {
        & $Python.Command @PythonArgs
    }
}

function Test-PythonModule {
    param(
        [hashtable]$Python,
        [string]$ModuleName
    )

    try {
        $null = Run-Python -Python $Python -PythonArgs @("-c", "import $ModuleName") 2>$null
    } catch {
        return $false
    }
    return $LASTEXITCODE -eq 0
}

function Install-Dependencies {
    $installerPath = Join-Path $PSScriptRoot "install_dependencies.ps1"
    if (-not (Test-Path -LiteralPath $installerPath)) {
        return $false
    }

    & $installerPath
    return $LASTEXITCODE -eq 0
}

# Returns the windowless interpreter (pythonw / pyw) next to the one we found.
function Find-WindowedPython {
    param([hashtable]$Python)

    if ($Python.Command -eq "py") {
        $pyw = Get-Command "pyw" -ErrorAction SilentlyContinue
        if ($null -ne $pyw) {
            return @{ Path = $pyw.Source; Prefix = @("-3") }
        }
        return $null
    }

    $cmd = Get-Command $Python.Command -ErrorAction SilentlyContinue
    if ($null -eq $cmd) {
        return $null
    }
    $candidate = Join-Path (Split-Path -Parent $cmd.Source) "pythonw.exe"
    if (Test-Path -LiteralPath $candidate) {
        return @{ Path = $candidate; Prefix = @() }
    }
    return $null
}

$guiPath = Join-Path $PSScriptRoot "zuvio_gui.py"
$python = Find-Python

if ($null -eq $python) {
    Write-Utf8Base64 "5om+5LiN5Yiw5ZWf5YuV55Kw5aKD77yM6KuL5YWI5Z+36KGM44CM56ys5LiA5qyh5L2/55SoLeWuieijneWll+S7ti5iYXTjgI3jgII="
    if (-not (Install-Dependencies)) {
        exit 1
    }
    $python = Find-Python
    if ($null -eq $python) {
        exit 1
    }
}

if (-not (Test-PythonModule -Python $python -ModuleName "requests")) {
    Write-Utf8Base64 "57y65bCR5b+F6KaB5aWX5Lu277yM5q2j5Zyo5YWI5Z+36KGM5a6J6KOdLi4u"
    if (-not (Install-Dependencies)) {
        exit 1
    }
    $python = Find-Python
    if (($null -eq $python) -or (-not (Test-PythonModule -Python $python -ModuleName "requests"))) {
        exit 1
    }
}

if (-not (Test-PythonModule -Python $python -ModuleName "tkinter")) {
    Write-Utf8Base64 "6YCZ5Y+w6Zu76IWm55qEIFB5dGhvbiDnvLrlsJHoppbnqpflhYPku7YgdGtpbnRlcuOAguiri+WIsCBweXRob24ub3JnIOmHjeaWsOWuieijnSBQeXRob27vvIzlronoo53mmYLli77pgbggdGNsL3Rr44CC"
    exit 1
}

$windowed = Find-WindowedPython -Python $python
if ($null -ne $windowed) {
    Write-Utf8Base64 "5q2j5Zyo6ZaL5ZWf6bue5ZCN6KaW56qXLi4u"
    $argList = @($windowed.Prefix) + @("`"$guiPath`"")
    Start-Process -FilePath $windowed.Path -ArgumentList $argList -WorkingDirectory $PSScriptRoot
    exit 0
}

# No pythonw available: run with the normal interpreter and keep this console open.
Write-Utf8Base64 "5om+5LiN5YiwIHB5dGhvbnfvvIzmlLnnlKjkuIDoiKzmqKHlvI/plovllZ/oppbnqpfvvIjpgJnlgIvpu5HoibLoppbnqpfoq4vlhYjliKXpl5zvvInjgII="
Run-Python -Python $python -PythonArgs @($guiPath)
exit $LASTEXITCODE
