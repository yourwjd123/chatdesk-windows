param([ValidateSet('Run','Build')][string]$Mode='Run')
$ErrorActionPreference = 'Stop'
try {
    $basePath = $PSScriptRoot
    $systemRoot = [IO.Path]::GetPathRoot($env:SystemRoot)
    $programRoot = [IO.Path]::GetPathRoot($basePath)
    if ($programRoot -eq $systemRoot -or $basePath.StartsWith('\\')) {
        throw 'Move the entire application folder to a local non-system drive first.'
    }
    # Resolve junction targets through Python before creating any runtime assets.
    $env:PYTHONDONTWRITEBYTECODE = '1'
    & python -B -c "import os,pathlib,sys; p=pathlib.Path(sys.argv[1]).resolve(); sys.exit(1 if not ((3,11)<=sys.version_info[:2]<(3,14)) or p.drive.lower()==os.environ.get('SystemDrive','C:').lower() or str(p).startswith(chr(92)*2) else 0)" $basePath
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11-3.13 is required, and the application must reside on a non-system local drive.' }
    Set-Location $basePath
    $runtimePath = Join-Path $basePath 'runtime'
    New-Item -ItemType Directory -Force $runtimePath | Out-Null
    $env:TEMP = $runtimePath
    $env:TMP = $runtimePath
    $env:PIP_NO_CACHE_DIR = '1'
    $env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $runtimePath 'pyinstaller'
    $env:XDG_CACHE_HOME = Join-Path $runtimePath 'cache'
    $env:XDG_CONFIG_HOME = Join-Path $runtimePath 'config'
    $env:XDG_DATA_HOME = Join-Path $runtimePath 'data'
    $pythonPath = Join-Path $basePath '.venv\Scripts\python.exe'
    if (!(Test-Path $pythonPath)) {
        & python -B -m venv (Join-Path $basePath '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Could not create local virtual environment.' }
    }
    & $pythonPath -B -m pip install --no-cache-dir -r (Join-Path $basePath 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    if ($Mode -eq 'Build') {
        & $pythonPath -B -m pip install --no-cache-dir pyinstaller==6.16.0
        if ($LASTEXITCODE -ne 0) { throw 'Build dependency installation failed.' }
        & $pythonPath -B -m PyInstaller --noconfirm --clean --onedir --windowed --name ChatDesk --distpath (Join-Path $basePath 'dist') --workpath (Join-Path $runtimePath 'build') --specpath $runtimePath (Join-Path $basePath 'app.py')
        if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
        Write-Host 'Portable application: dist\ChatDesk\ChatDesk.exe . Copy the ENTIRE ChatDesk folder.'
    } else {
        & $pythonPath -B (Join-Path $basePath 'app.py')
        if ($LASTEXITCODE -ne 0) { throw 'Application exited unexpectedly.' }
    }
} catch {
    # Do not print exception objects: this script never handles credentials.
    Write-Host 'Setup/launch failed. Use a non-system drive, Python 3.11-3.13, and check network connectivity.'
    exit 1
}
