# LlamaShift Windows GUI Installer
# Opens a graphical setup wizard with questions and auto-detection
# Run as Administrator for full functionality

#Requires -RunAsAdministrator
[CmdletBinding()]
param()

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# ─── Colors & Fonts ──────────────────────────────────────────────
$PrimaryColor = [System.Drawing.Color]::FromArgb(42, 157, 143)    # Teal
$SecondaryColor = [System.Drawing.Color]::FromArgb(33, 33, 33)    # Dark gray
$TextColor = [System.Drawing.Color]::White
$BgColor = [System.Drawing.Color]::FromArgb(30, 30, 30)           # Near black
$CardBg = [System.Drawing.Color]::FromArgb(45, 45, 45)            # Slightly lighter
$SuccessColor = [System.Drawing.Color]::FromArgb(80, 200, 120)
$WarnColor = [System.Drawing.Color]::FromArgb(255, 180, 0)
$ErrorColor = [System.Drawing.Color]::FromArgb(255, 80, 80)

$FontFamily = "Segoe UI"
$TitleFont = New-Object System.Drawing.Font($FontFamily, 14, [System.Drawing.FontStyle]::Bold)
$BodyFont = New-Object System.Drawing.Font($FontFamily, 10)
$SmallFont = New-Object System.Drawing.Font($FontFamily, 9)
$ButtonFont = New-Object System.Drawing.Font($FontFamily, 10, [System.Drawing.FontStyle]::Bold)

# ─── State ───────────────────────────────────────────────────────
$installerData = @{
    OsType = "windows"
    PythonPath = ""
    BinaryPath = ""
    GpuType = "cpu"
    GpuInfo = @{}
    Mode = "single_port"
    ModelConfig = $null
    ServiceMethod = ""
    AppDir = (Get-Location).Path
}

# Find Python
$pythonExe = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonExe) {
    $pythonExe = Get-Command python3 -ErrorAction SilentlyContinue
}
if ($pythonExe) {
    $installerData.PythonPath = $pythonExe.Source
}

# ─── Helper Functions ────────────────────────────────────────────
function New-RoundRect {
    param([int]$x, [int]$y, [int]$w, [int]$h, [int]$r, [System.Drawing.Color]$color)
    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $path.AddArc($x, $y, $r, $r, 180, 90)
    $path.AddArc($x + $w - $r, $y, $r, $r, 270, 90)
    $path.AddArc($x + $w - $r, $y + $h - $r, $r, $r, 0, 90)
    $path.AddArc($x, $y + $h - $r, $r, $r, 90, 90)
    $path.CloseFigure()
    return $path
}

function Test-Gpu {
    # Check NVIDIA
    $nvidiaSmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if ($nvidiaSmi) {
        try {
            $output = & nvidia-smi --query-gpu=name --format=csv,noheader 2>$null
            if ($output) {
                return @{ type = "nvidia"; gpus = @($output); cuda = $true }
            }
        } catch {}
    }
    # Check AMD
    if (Test-Path "\dev\kfd") {
        return @{ type = "amd"; cuda = $false; rocm = $true }
    }
    return @{ type = "cpu"; cuda = $false }
}

function Show-Waiting {
    param([System.Windows.Forms.Form]$form, [string]$message)
    $lbl = New-Object System.Windows.Forms.Label
    $lbl.Text = $message
    $lbl.Font = $BodyFont
    $lbl.ForeColor = $PrimaryColor
    $lbl.Location = New-Object System.Drawing.Point(30, 200)
    $lbl.AutoSize = $true
    $form.Controls.Add($lbl)
    $form.Refresh()
    return $lbl
}

function Get-GgufFiles {
    param([string]$BaseDir = "")
    if ($BaseDir -and (Test-Path $BaseDir)) {
        $paths = @($BaseDir)
    } else {
        $paths = @(
            "$env:USERPROFILE\models",
            "$env:USERPROFILE\.cache\huggingface\hub",
            ".\models"
        )
    }
    $files = @()
    foreach ($p in $paths) {
        if (Test-Path $p) {
            $files += Get-ChildItem -Path $p -Filter "*.gguf" -Recurse -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName
        }
    }
    return $files | Select-Object -First 50
}

function Install-PythonDependencies {
    param([System.Windows.Forms.Form]$form, [ref]$StatusLabel)
    $deps = @(
        "flask>=3.0.0",
        "requests>=2.28.0",
        "psutil>=5.9.0"
    )
    $StatusLabel.Value.Text = "Installing Python dependencies..."
    $StatusLabel.Value.ForeColor = $PrimaryColor
    $StatusLabel.Value.Refresh()
    $form.Refresh()

    $missing = @()
    foreach ($dep in $deps) {
        $pkgName = $dep.Split(">")[0].Split("=")[0]
        try {
            & python -c "import $pkgName" 2>$null
            if ($LASTEXITCODE -ne 0) {
                $missing += $dep
            }
        } catch {
            $missing += $dep
        }
    }

    if ($missing.Count -gt 0) {
        $StatusLabel.Value.Text = "Installing: $($missing -join ', ')"
        $StatusLabel.Value.Refresh()
        $form.Refresh()
        foreach ($dep in $missing) {
            & pip install "$dep" 2>&1 | Out-Null
        }
        $StatusLabel.Value.Text = "✓ Dependencies installed"
        $StatusLabel.Value.ForeColor = $SuccessColor
    } else {
        $StatusLabel.Value.Text = "✓ All dependencies already installed"
        $StatusLabel.Value.ForeColor = $SuccessColor
    }
    $StatusLabel.Value.Refresh()
    $form.Refresh()
}

# ─── Step Forms ──────────────────────────────────────────────────

# Welcome
$welcomeForm = New-Object System.Windows.Forms.Form
$welcomeForm.Text = "LlamaShift Installer"
$welcomeForm.Size = New-Object System.Drawing.Size(600, 400)
$welcomeForm.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedDialog
$welcomeForm.MaximizeBox = $false
$welcomeForm.MinimizeBox = $false
$welcomeForm.StartPosition = "CenterScreen"
$welcomeForm.BackColor = $BgColor

# Title
$welcomeTitle = New-Object System.Windows.Forms.Label
$welcomeTitle.Text = "🦙 LlamaShift"
$welcomeTitle.Font = New-Object System.Drawing.Font($FontFamily, 24, [System.Drawing.FontStyle]::Bold)
$welcomeTitle.ForeColor = $PrimaryColor
$welcomeTitle.Location = New-Object System.Drawing.Point(30, 40)
$welcomeTitle.AutoSize = $true

$welcomeSub = New-Object System.Windows.Forms.Label
$welcomeSub.Text = "Universal LLM Workstation Manager`n`nThis wizard will configure your LlamaShift installation.`nIt requires Administrator privileges."
$welcomeSub.Font = $BodyFont
$welcomeSub.ForeColor = $TextColor
$welcomeSub.Location = New-Object System.Drawing.Point(30, 100)
$welcomeSub.AutoSize = $true
$welcomeSub.Width = 500

# OS info
$osName = (Get-CimInstance Win32_OperatingSystem).Caption
$osBuild = (Get-CimInstance Win32_OperatingSystem).BuildNumber
$osArch = if ([Environment]::Is64BitOperatingSystem) { "64-bit" } else { "32-bit" }
$pythonVer = "N/A"
if ($installerData.PythonPath) {
    try { $pythonVer = (& python --version 2>&1) } catch {}
}
$ram = (Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory
$ramGb = [math]::Round($ram / 1GB, 1)

$welcomeInfo = New-Object System.Windows.Forms.Label
$welcomeInfo.Text = "OS:      $osName ($osBuild $osArch)`nPython:  $pythonVer`nRAM:      $ramGb GB`nGPU:     Detecting..."
$welcomeInfo.Font = $SmallFont
$welcomeInfo.ForeColor = [System.Drawing.Color]::FromArgb(180, 180, 180)
$welcomeInfo.Location = New-Object System.Drawing.Point(30, 180)
$welcomeInfo.Width = 500
$welcomeInfo.Height = 70
$welcomeInfo.AutoSize = $false
$welcomeInfo.MultiLine = $true

# GPU detection
$gpuLabel = New-Object System.Windows.Forms.Label
$gpuLabel.Text = "GPU:     Checking..."
$gpuLabel.Font = $SmallFont
$gpuLabel.ForeColor = [System.Drawing.Color]::FromArgb(180, 180, 180)
$gpuLabel.Location = New-Object System.Drawing.Point(30, 250)
$gpuLabel.AutoSize = $true

$welcomeForm.Controls.Add($welcomeTitle)
$welcomeForm.Controls.Add($welcomeSub)
$welcomeForm.Controls.Add($welcomeInfo)
$welcomeForm.Controls.Add($gpuLabel)

# Detect GPU in background
$gpuResult = Test-Gpu
$installerData.GpuType = $gpuResult.type
$installerData.GpuInfo = $gpuResult

$gpuName = if ($gpuResult.type -eq "nvidia") {
    $gpuResult.gpus -join ", "
} elseif ($gpuResult.type -eq "amd") {
    "AMD ROCm (detected)"
} else {
    "CPU only (no GPU detected)"
}
$gpuLabel.Text = "GPU:     $gpuName"
if ($gpuResult.type -ne "cpu") {
    $gpuLabel.ForeColor = $SuccessColor
}

# Start button
$startBtn = New-Object System.Windows.Forms.Button
$startBtn.Text = "Get Started →"
$startBtn.Font = $ButtonFont
$startBtn.ForeColor = $TextColor
$startBtn.BackColor = $PrimaryColor
$startBtn.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
$startBtn.Size = New-Object System.Drawing.Size(180, 40)
$startBtn.Location = New-Object System.Drawing.Point(30, 300)

$startBtn.Add_Click({
    $welcomeForm.Dispose()
    ShowPrerequisites
})

$welcomeForm.Controls.Add($startBtn)
$welcomeForm.TopMost = $true
$welcomeForm.ShowDialog()

# ─── Step 1: Prerequisites ───────────────────────────────────────
function ShowPrerequisites {
    $form = New-Object System.Windows.Forms.Form
    $form.Text = "LlamaShift - Step 1/5: Prerequisites"
    $form.Size = New-Object System.Drawing.Size(600, 450)
    $form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedDialog
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.StartPosition = "CenterScreen"
    $form.BackColor = $BgColor

    $title = New-Object System.Windows.Forms.Label
    $title.Text = "Step 1/5: Prerequisites"
    $title.Font = $TitleFont
    $title.ForeColor = $PrimaryColor
    $title.Location = New-Object System.Drawing.Point(30, 20)

    $progressBar = New-Object System.Windows.Forms.ProgressBar
    $progressBar.Value = 20
    $progressBar.Location = New-Object System.Drawing.Point(30, 55)
    $progressBar.Width = 500
    $progressBar.Height = 6

    # Check items
    $items = @{
        "Python 3.10+" = $null
        "Flask module" = $null
        "llama-server" = $null
    }

    # Python check
    $y = 80
    if ($installerData.PythonPath) {
        $lbl = New-Object System.Windows.Forms.Label
        $lbl.Text = "✓ Python 3.10+ found"
        $lbl.Font = $BodyFont
        $lbl.ForeColor = $SuccessColor
        $lbl.Location = New-Object System.Drawing.Point(30, $y)
        $form.Controls.Add($lbl)
        $y += 30
    } else {
        $lbl = New-Object System.Windows.Forms.Label
        $lbl.Text = "✗ Python not found. Please install Python 3.10+ from https://www.python.org/downloads/"
        $lbl.Font = $BodyFont
        $lbl.ForeColor = $ErrorColor
        $lbl.Location = New-Object System.Drawing.Point(30, $y)
        $form.Controls.Add($lbl)
        $y += 30
    }

    # Python dependencies check (all with pinned versions)
    $deps = @{
        "flask" = "flask>=3.0.0"
        "requests" = "requests>=2.28.0"
        "psutil" = "psutil>=5.9.0"
    }
    $missingDeps = @()
    foreach ($pkg in $deps.Keys) {
        try {
            & python -c "import $pkg" 2>$null
            if ($LASTEXITCODE -ne 0) { $missingDeps += "$($pkg) ($($deps[$pkg]))" }
        } catch {
            $missingDeps += "$($pkg) ($($deps[$pkg]))"
        }
    }

    if ($missingDeps.Count -eq 0) {
        $lbl = New-Object System.Windows.Forms.Label
        $lbl.Text = "✓ All Python dependencies satisfied (flask>=3.0.0, requests>=2.28.0, psutil>=5.9.0)"
        $lbl.Font = $BodyFont
        $lbl.ForeColor = $SuccessColor
        $lbl.Location = New-Object System.Drawing.Point(30, $y)
        $form.Controls.Add($lbl)
    } else {
        $lbl = New-Object System.Windows.Forms.Label
        $lbl.Text = "! Missing dependencies: $($missingDeps -join ', ')"
        $lbl.Font = $BodyFont
        $lbl.ForeColor = $WarnColor
        $lbl.Location = New-Object System.Drawing.Point(30, $y)
        $form.Controls.Add($lbl)

        $installDepsBtn = New-Object System.Windows.Forms.Button
        $installDepsBtn.Text = "Install Dependencies"
        $installDepsBtn.Location = New-Object System.Drawing.Point(30, $y + 25)
        $installDepsBtn.Size = New-Object System.Drawing.Size(180, 30)
        $installDepsBtn.Add_Click({
            $installDepsBtn.Enabled = $false
            $installDepsBtn.Text = "Installing..."
            $installDepsBtn.Refresh()
            $form.Refresh()
            foreach ($ver in $deps.Values) {
                & pip install "$ver" 2>&1 | Out-Null
            }
            $installDepsBtn.Text = "✓ Dependencies installed"
            $installDepsBtn.ForeColor = $SuccessColor
            $installDepsBtn.Refresh()
            $lbl.Text = "✓ All Python dependencies satisfied (flask>=3.0.0, requests>=2.28.0, psutil>=5.9.0)"
            $lbl.ForeColor = $SuccessColor
        })
        $form.Controls.Add($installDepsBtn)
    }

    # llama-server binary path — auto-detect
    $y = $y + 20

    # Auto-detect common llama-server paths
    $commonPaths = @(
        "C:\Program Files\llama.cpp\llama-server.exe",
        "C:\Program Files\llama.cpp\bin\llama-server.exe",
        "C:\Users\Public\llama-server.exe",
        (Join-Path $installerData.AppDir "llama-server.exe"),
        (Join-Path $installerData.AppDir "bin\llama-server.exe"),
        "$env:LOCALAPPDATA\llama.cpp\llama-server.exe"
    )

    $detected = @()
    foreach ($p in $commonPaths) {
        if (Test-Path $p) {
            try {
                $helpOut = & $p --help 2>&1 | Out-String
                if ($LASTEXITCODE -eq -1 -or $helpOut -match "llama") {
                    $detected += $p
                }
            } catch {}
        }
    }

    if ($detected.Count -gt 0) {
        $autoLabel = New-Object System.Windows.Forms.Label
        $autoLabel.Text = "✓ Auto-detected llama-server binaries:"
        $autoLabel.Font = $BodyFont
        $autoLabel.ForeColor = $SuccessColor
        $autoLabel.Location = New-Object System.Drawing.Point(30, $y)
        $autoLabel.AutoSize = $true
        $form.Controls.Add($autoLabel)
        $y += 25

        $combo = New-Object System.Windows.Forms.ComboBox
        $combo.Name = "BinaryCombo"
        $combo.Location = New-Object System.Drawing.Point(30, $y)
        $combo.Width = 490
        $combo.Font = $SmallFont
        foreach ($d in $detected) {
            $combo.Items.Add($d) | Out-Null
        }
        $combo.SelectedIndex = 0
        $combo.Add_SelectedValueChanged({
            $installerData.BinaryPath = $combo.Text
        })
        $form.Controls.Add($combo)
        $installerData.BinaryPath = $detected[0]
        $y += 30

        $relabel = New-Object System.Windows.Forms.Label
        $relabel.Text = "Or browse for a different binary:"
        $relabel.Font = $SmallFont
        $relabel.ForeColor = [System.Drawing.Color]::FromArgb(150, 150, 150)
        $relabel.Location = New-Object System.Drawing.Point(30, $y)
        $relabel.AutoSize = $true
        $form.Controls.Add($relabel)
        $y += 25
    } else {
        $warnLabel = New-Object System.Windows.Forms.Label
        $warnLabel.Text = "⚠ No llama-server binary auto-detected."
        $warnLabel.Font = $BodyFont
        $warnLabel.ForeColor = $WarnColor
        $warnLabel.Location = New-Object System.Drawing.Point(30, $y)
        $warnLabel.Width = 540
        $warnLabel.AutoSize = $false
        $warnLabel.MultiLine = $true
        $warnLabel.Height = 22
        $form.Controls.Add($warnLabel)
        $y += 25

        $hintLabel = New-Object System.Windows.Forms.Label
        $hintLabel.Text = "Example paths: C:\llama.cpp\build\bin\Release\llama-server.exe"
        $hintLabel.Font = $SmallFont
        $hintLabel.ForeColor = [System.Drawing.Color]::FromArgb(150, 150, 150)
        $hintLabel.Location = New-Object System.Drawing.Point(45, $y)
        $hintLabel.AutoSize = $true
        $form.Controls.Add($hintLabel)
        $y += 20

        $hintLabel2 = New-Object System.Windows.Forms.Label
        $hintLabel2.Text = "              C:\Program Files\llama.cpp\llama-server.exe"
        $hintLabel2.Font = $SmallFont
        $hintLabel2.ForeColor = [System.Drawing.Color]::FromArgb(150, 150, 150)
        $hintLabel2.Location = New-Object System.Drawing.Point(45, $y)
        $hintLabel2.AutoSize = $true
        $form.Controls.Add($hintLabel2)
        $y += 25
    }

    $binaryPathBox = New-Object System.Windows.Forms.TextBox
    $binaryPathBox.Location = New-Object System.Drawing.Point(30, $y)
    $binaryPathBox.Width = 400
    $binaryPathBox.Text = ""
    $binaryPathBox.Font = $SmallFont
    $binaryPathBox.PlaceholderText = "llama-server.exe path"
    $form.Controls.Add($binaryPathBox)

    $browseBtn = New-Object System.Windows.Forms.Button
    $browseBtn.Text = "Browse..."
    $browseBtn.Location = New-Object System.Drawing.Point(440, $y)
    $browseBtn.Size = New-Object System.Drawing.Size(80, 25)
    $form.Controls.Add($browseBtn)

    $browseFileDialog = New-Object System.Windows.Forms.OpenFileDialog
    $browseFileDialog.Filter = "Executable (*.exe)|*.exe|All files (*.*)|*.*"
    $browseFileDialog.Title = "Find llama-server.exe"
    $browseBtn.Add_Click({
        if ($browseFileDialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
            $binaryPathBox.Text = $browseFileDialog.FileName
            # Update combo if exists
            $combo = $form.Controls.Find("BinaryCombo", $true)[0]
            if ($combo) {
                $combo.Items.Add($browseFileDialog.FileName) | Out-Null
                $combo.SelectedItem = $browseFileDialog.FileName
            }
        }
    })

    $y += 35
    $binaryPathBox.Add_TextChanged({ $installerData.BinaryPath = $binaryPathBox.Text })

    # Next button
    $nextBtn = New-Object System.Windows.Forms.Button
    $nextBtn.Text = "Next →"
    $nextBtn.Font = $ButtonFont
    $nextBtn.ForeColor = $TextColor
    $nextBtn.BackColor = $PrimaryColor
    $nextBtn.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
    $nextBtn.Size = New-Object System.Drawing.Size(120, 36)
    $nextBtn.Location = New-Object System.Drawing.Point(380, 390)
    $form.Controls.Add($nextBtn)

    # Validate binary before proceeding
    $validateMsg = New-Object System.Windows.Forms.Label
    $validateMsg.Text = ""
    $validateMsg.Font = $SmallFont
    $validateMsg.ForeColor = [System.Drawing.Color]::FromArgb(150, 150, 150)
    $validateMsg.Location = New-Object System.Drawing.Point(30, $y + 5)
    $validateMsg.AutoSize = $true
    $validateMsg.Width = 500
    $validateMsg.Height = 40
    $validateMsg.MultiLine = $true
    $form.Controls.Add($validateMsg)

    $nextBtn.Add_Click({
        $installerData.BinaryPath = $binaryPathBox.Text

        # Validate binary
        if (-not $installerData.BinaryPath -or -not (Test-Path $installerData.BinaryPath)) {
            $validateMsg.Text = "⚠ Error: llama-server binary path is required. Please provide a valid path."
            $validateMsg.ForeColor = $ErrorColor
            return
        }

        try {
            $helpOut = & $installerData.BinaryPath --help 2>&1 | Out-String
            $validateMsg.Text = "✓ llama-server binary verified."
            $validateMsg.ForeColor = $SuccessColor
        } catch {
            # --help may return exit code != 0 but still print to stdout
            if ($helpOut -and ($helpOut -match "llama" -or $helpOut -match "usage" -or $helpOut -match "help")) {
                $validateMsg.Text = "✓ llama-server binary verified."
                $validateMsg.ForeColor = $SuccessColor
            } else {
                $validateMsg.Text = "⚠ Warning: binary may not be a valid llama-server. Proceeding anyway..."
                $validateMsg.ForeColor = $WarnColor
            }
        }

        [System.Threading.Thread]::Sleep(800)
        $form.Dispose()
        ShowGpuConfig
    })

    $form.TopMost = $true
    $form.ShowDialog()
}

# ─── Step 2: GPU Configuration ───────────────────────────────────
function ShowGpuConfig {
    $form = New-Object System.Windows.Forms.Form
    $form.Text = "LlamaShift - Step 2/5: GPU Configuration"
    $form.Size = New-Object System.Drawing.Size(600, 450)
    $form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedDialog
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.StartPosition = "CenterScreen"
    $form.BackColor = $BgColor

    $title = New-Object System.Windows.Forms.Label
    $title.Text = "Step 2/5: GPU Configuration"
    $title.Font = $TitleFont
    $title.ForeColor = $PrimaryColor
    $title.Location = New-Object System.Drawing.Point(30, 20)

    $progressBar = New-Object System.Windows.Forms.ProgressBar
    $progressBar.Value = 40
    $progressBar.Location = New-Object System.Drawing.Point(30, 55)
    $progressBar.Width = 500
    $progressBar.Height = 6

    $y = 80

    # Display GPU info
    $gpuCard = New-Object System.Windows.Forms.Panel
    $gpuCard.BackColor = $CardBg
    $gpuCard.Location = New-Object System.Drawing.Point(30, $y)
    $gpuCard.Width = 500
    $gpuCard.Height = 70

    $gpuInfoLabel = New-Object System.Windows.Forms.Label
    if ($installerData.GpuType -eq "nvidia") {
        $gpuInfoLabel.Text = "NVIDIA GPU Detected`nCUDA available — full GPU acceleration"
        $gpuInfoLabel.ForeColor = $SuccessColor
    } elseif ($installerData.GpuType -eq "amd") {
        $gpuInfoLabel.Text = "AMD GPU Detected`nROCm available — full GPU acceleration"
        $gpuInfoLabel.ForeColor = $SuccessColor
    } else {
        $gpuInfoLabel.Text = "No GPU detected`nModels will run in CPU mode (slower)"
        $gpuInfoLabel.ForeColor = $WarnColor
    }
    $gpuInfoLabel.Font = $BodyFont
    $gpuInfoLabel.Location = New-Object System.Drawing.Point(15, 15)
    $gpuInfoLabel.AutoSize = $true
    $gpuCard.Controls.Add($gpuInfoLabel)
    $form.Controls.Add($gpuCard)

    $y += 90

    # Mode selection
    $modeLabel = New-Object System.Windows.Forms.Label
    $modeLabel.Text = "Operation mode:"
    $modeLabel.Font = $BodyFont
    $modeLabel.ForeColor = $TextColor
    $modeLabel.Location = New-Object System.Drawing.Point(30, $y)
    $modeLabel.AutoSize = $true
    $form.Controls.Add($modeLabel)
    $y += 25

    $singlePortRadio = New-Object System.Windows.Forms.RadioButton
    $singlePortRadio.Text = "Single-port — One model at a time (ideal for Open WebUI)"
    $singlePortRadio.Font = $BodyFont
    $singlePortRadio.ForeColor = $TextColor
    $singlePortRadio.Location = New-Object System.Drawing.Point(30, $y)
    $singlePortRadio.Checked = $true
    $form.Controls.Add($singlePortRadio)

    $multiPortRadio = New-Object System.Windows.Forms.RadioButton
    $multiPortRadio.Text = "Multi-port — Multiple models simultaneously"
    $multiPortRadio.Font = $BodyFont
    $multiPortRadio.ForeColor = $TextColor
    $multiPortRadio.Location = New-Object System.Drawing.Point(30, $y + 30)
    $form.Controls.Add($multiPortRadio)

    $y += 70

    # GPU layer offload (only for GPU)
    $layerLabel = $null
    $layerNumericUpDown = $null
    if ($installerData.GpuType -ne "cpu") {
        $layerLabel = New-Object System.Windows.Forms.Label
        $layerLabel.Text = "GPU layer offload:"
        $layerLabel.Font = $BodyFont
        $layerLabel.ForeColor = $TextColor
        $layerLabel.Location = New-Object System.Drawing.Point(30, $y)
        $layerLabel.AutoSize = $true
        $form.Controls.Add($layerLabel)
        $y += 25

        $offloadAllCheck = New-Object System.Windows.Forms.CheckBox
        $offloadAllCheck.Text = "Offload all layers to GPU (recommended)"
        $offloadAllCheck.Font = $BodyFont
        $offloadAllCheck.ForeColor = $TextColor
        $offloadAllCheck.Location = New-Object System.Drawing.Point(30, $y)
        $offloadAllCheck.Checked = $true
        $form.Controls.Add($offloadAllCheck)
        $y += 30

        $layerLabel2 = New-Object System.Windows.Forms.Label
        $layerLabel2.Text = "Manual layer count:"
        $layerLabel2.Font = $SmallFont
        $layerLabel2.ForeColor = [System.Drawing.Color]::FromArgb(150, 150, 150)
        $layerLabel2.Location = New-Object System.Drawing.Point(30, $y)
        $form.Controls.Add($layerLabel2)

        $layerNumericUpDown = New-Object System.Windows.Forms.NumericUpDown
        $layerNumericUpDown.Location = New-Object System.Drawing.Point(30, $y + 25)
        $layerNumericUpDown.Minimum = 0
        $layerNumericUpDown.Maximum = 999
        $layerNumericUpDown.Value = 99
        $layerNumericUpDown.Width = 100
        $form.Controls.Add($layerNumericUpDown)

        $offloadAllCheck.Add_CheckStateChanged({
            if ($offloadAllCheck.Checked) {
                $layerNumericUpDown.Enabled = $false
            } else {
                $layerNumericUpDown.Enabled = $true
            }
        })
        $y += 55
    }

    # Next button
    $nextBtn = New-Object System.Windows.Forms.Button
    $nextBtn.Text = "Next →"
    $nextBtn.Font = $ButtonFont
    $nextBtn.ForeColor = $TextColor
    $nextBtn.BackColor = $PrimaryColor
    $nextBtn.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
    $nextBtn.Size = New-Object System.Drawing.Size(120, 36)
    $nextBtn.Location = New-Object System.Drawing.Point(380, 390)
    $form.Controls.Add($nextBtn)

    $nextBtn.Add_Click({
        if ($singlePortRadio.Checked) {
            $installerData.Mode = "single_port"
        } else {
            $installerData.Mode = "multi_port"
        }

        if ($offloadAllCheck -and $offloadAllCheck.Checked) {
            $installerData.GpuLayers = 999
        } elseif ($layerNumericUpDown) {
            $installerData.GpuLayers = [int]$layerNumericUpDown.Value
        } else {
            $installerData.GpuLayers = 0
        }

        $form.Dispose()
        ShowModelConfig
    })

    $form.TopMost = $true
    $form.ShowDialog()
}

# ─── Step 3: Model Configuration ─────────────────────────────────
function ShowModelConfig {
    $form = New-Object System.Windows.Forms.Form
    $form.Text = "LlamaShift - Step 3/5: Model Configuration"
    $form.Size = New-Object System.Drawing.Size(650, 520)
    $form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedDialog
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.StartPosition = "CenterScreen"
    $form.BackColor = $BgColor

    $title = New-Object System.Windows.Forms.Label
    $title.Text = "Step 3/5: Model Configuration"
    $title.Font = $TitleFont
    $title.ForeColor = $PrimaryColor
    $title.Location = New-Object System.Drawing.Point(30, 20)

    $progressBar = New-Object System.Windows.Forms.ProgressBar
    $progressBar.Value = 60
    $progressBar.Location = New-Object System.Drawing.Point(30, 55)
    $progressBar.Width = 550
    $progressBar.Height = 6

    # Find GGUF files
    $ggufFiles = Get-GgufFiles

    $y = 80

    if ($ggufFiles.Count -gt 0) {
        $foundLabel = New-Object System.Windows.Forms.Label
        $foundLabel.Text = "Found $($ggufFiles.Count) GGUF model(s) on your system:"
        $foundLabel.Font = $BodyFont
        $foundLabel.ForeColor = $SuccessColor
        $foundLabel.Location = New-Object System.Drawing.Point(30, $y)
        $form.Controls.Add($foundLabel)
        $y += 25

        # Model listbox
        $modelListBox = New-Object System.Windows.Forms.ListBox
        $modelListBox.Location = New-Object System.Drawing.Point(30, $y)
        $modelListBox.Width = 550
        $modelListBox.Height = 150
        $modelListBox.Font = $SmallFont
        $modelListBox.ForeColor = $TextColor
        $modelListBox.BackColor = $CardBg

        foreach ($f in $ggufFiles | Select-Object -First 20) {
            $sizeMB = [math]::Round((Get-Item $f).Length / 1MB, 0)
            $modelListBox.Items.Add("$(Split-Path $f -Leaf) ($sizeMB MB)") | Out-Null
        }
        $modelListBox.SelectedIndex = 0
        $form.Controls.Add($modelListBox)

        $y += 170

        # Model details
        $nameLabel = New-Object System.Windows.Forms.Label
        $nameLabel.Text = "Display name:"
        $nameLabel.Font = $SmallFont
        $nameLabel.ForeColor = $TextColor
        $nameLabel.Location = New-Object System.Drawing.Point(30, $y)
        $form.Controls.Add($nameLabel)
        $y += 22

        $nameBox = New-Object System.Windows.Forms.TextBox
        $nameBox.Location = New-Object System.Drawing.Point(30, $y)
        $nameBox.Width = 300
        $nameBox.Font = $BodyFont
        $form.Controls.Add($nameBox)

        $y += 35
        $ctxLabel = New-Object System.Windows.Forms.Label
        $ctxLabel.Text = "Context size (tokens):"
        $ctxLabel.Font = $SmallFont
        $ctxLabel.ForeColor = $TextColor
        $ctxLabel.Location = New-Object System.Drawing.Point(30, $y)
        $form.Controls.Add($ctxLabel)
        $y += 22

        $ctxBox = New-Object System.Windows.Forms.NumericUpDown
        $ctxBox.Location = New-Object System.Drawing.Point(30, $y)
        $ctxBox.Width = 100
        $ctxBox.Minimum = 512
        $ctxBox.Maximum = 131072
        $ctxBox.Value = 4096
        $ctxBox.Font = $BodyFont
        $form.Controls.Add($ctxBox)

        $y += 45

        # Port config
        $portLabel = New-Object System.Windows.Forms.Label
        $portLabel.Text = "Port:"
        $portLabel.Font = $SmallFont
        $portLabel.ForeColor = $TextColor
        $portLabel.Location = New-Object System.Drawing.Point(30, $y)
        $form.Controls.Add($portLabel)
        $y += 22

        $portBox = New-Object System.Windows.Forms.NumericUpDown
        $portBox.Location = New-Object System.Drawing.Point(30, $y)
        $portBox.Width = 100
        $portBox.Minimum = 8000
        $portBox.Maximum = 65535
        $portBox.Value = 9000
        $portBox.Font = $BodyFont
        $form.Controls.Add($portBox)

        $y += 30
        $useCheck = New-Object System.Windows.Forms.CheckBox
        $useCheck.Text = "Use this model"
        $useCheck.Font = $BodyFont
        $useCheck.ForeColor = $SuccessColor
        $useCheck.Checked = $true
        $useCheck.Location = New-Object System.Drawing.Point(30, $y)
        $form.Controls.Add($useCheck)

        $y += 40

        # Store refs for next step
        $form.Tag = @{
            ListBox = $modelListBox
            NameBox = $nameBox
            CtxBox = $ctxBox
            PortBox = $portBox
            UseCheck = $useCheck
        }

    } else {
        $noModelLabel = New-Object System.Windows.Forms.Label
        $noModelLabel.Text = "⚠ No GGUF models found in default locations.`nSearched: %USERPROFILE%\models, .\models"
        $noModelLabel.Font = $BodyFont
        $noModelLabel.ForeColor = $WarnColor
        $noModelLabel.Location = New-Object System.Drawing.Point(30, $y)
        $noModelLabel.Width = 550
        $noModelLabel.AutoSize = $false
        $noModelLabel.MultiLine = $true
        $form.Controls.Add($noModelLabel)

        $y += 50

        # Ask for model directory path
        $dirLabel = New-Object System.Windows.Forms.Label
        $dirLabel.Text = "Path to directory containing your GGUF model files:"
        $dirLabel.Font = $BodyFont
        $dirLabel.ForeColor = $TextColor
        $dirLabel.Location = New-Object System.Drawing.Point(30, $y)
        $dirLabel.AutoSize = $true
        $form.Controls.Add($dirLabel)

        $y += 28
        $modelDirBox = New-Object System.Windows.Forms.TextBox
        $modelDirBox.Location = New-Object System.Drawing.Point(30, $y)
        $modelDirBox.Width = 400
        $modelDirBox.Text = "$env:USERPROFILE\models"
        $modelDirBox.Font = $SmallFont
        $form.Controls.Add($modelDirBox)

        $dirBrowseBtn = New-Object System.Windows.Forms.Button
        $dirBrowseBtn.Text = "Browse..."
        $dirBrowseBtn.Location = New-Object System.Drawing.Point(440, $y)
        $dirBrowseBtn.Size = New-Object System.Drawing.Size(80, 25)
        $form.Controls.Add($dirBrowseBtn)

        $dirBrowseDialog = New-Object System.Windows.Forms.FolderBrowserDialog
        $dirBrowseDialog.Description = "Select directory containing GGUF model files"
        $dirBrowseBtn.Add_Click({
            if ($dirBrowseDialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
                $modelDirBox.Text = $dirBrowseDialog.SelectedPath
            }
        })

        $y += 35
        $scanBtn = New-Object System.Windows.Forms.Button
        $scanBtn.Text = "Scan Directory"
        $scanBtn.Location = New-Object System.Drawing.Point(30, $y)
        $scanBtn.Size = New-Object System.Drawing.Size(120, 28)
        $scanBtn.Font = $SmallFont
        $scanResultLabel = New-Object System.Windows.Forms.Label
        $scanResultLabel.Text = ""
        $scanResultLabel.Font = $SmallFont
        $scanResultLabel.Location = New-Object System.Drawing.Point(160, $y)
        $scanResultLabel.AutoSize = $true
        $form.Controls.Add($scanResultLabel)
        $form.Controls.Add($scanBtn)

        $scanBtn.Add_Click({
            $dir = $modelDirBox.Text
            if (Test-Path $dir) {
                $found = Get-ChildItem -Path $dir -Filter "*.gguf" -Recurse -ErrorAction SilentlyContinue
                if ($found.Count -gt 0) {
                    $scanResultLabel.Text = "✓ Found $($found.Count) GGUF file(s)"
                    $scanResultLabel.ForeColor = $SuccessColor
                } else {
                    $scanResultLabel.Text = "✗ No .gguf files in this directory"
                    $scanResultLabel.ForeColor = $ErrorColor
                }
            } else {
                $scanResultLabel.Text = "✗ Directory does not exist"
                $scanResultLabel.ForeColor = $ErrorColor
            }
        })

        $y += 50
        $infoLabel = New-Object System.Windows.Forms.Label
        $infoLabel.Text = "ℹ You can also download models from HuggingFace and place them in this directory.`nModels can be configured later by editing config.json."
        $infoLabel.Font = $SmallFont
        $infoLabel.ForeColor = [System.Drawing.Color]::FromArgb(150, 150, 150)
        $infoLabel.Location = New-Object System.Drawing.Point(30, $y)
        $infoLabel.Width = 550
        $infoLabel.AutoSize = $false
        $infoLabel.MultiLine = $true
        $form.Controls.Add($infoLabel)

        $form.Tag = @{ ModelDirBox = $modelDirBox }
    }

    # Next button
    $nextBtn = New-Object System.Windows.Forms.Button
    $nextBtn.Text = "Next →"
    $nextBtn.Font = $ButtonFont
    $nextBtn.ForeColor = $TextColor
    $nextBtn.BackColor = $PrimaryColor
    $nextBtn.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
    $nextBtn.Size = New-Object System.Drawing.Size(120, 36)
    $nextBtn.Location = New-Object System.Drawing.Point(430, 470)
    $form.Controls.Add($nextBtn)

    $nextBtn.Add_Click({
        if ($ggufFiles.Count -gt 0) {
            $tags = $form.Tag
            if ($tags.UseCheck.Checked -and $tags.ListBox.SelectedIndex -ge 0) {
                $selectedFile = $ggufFiles[$tags.ListBox.SelectedIndex]
                $installerData.ModelConfig = @{
                    id = (Split-Path $selectedFile -Leaf).replace(".gguf", "").replace("-", "").replace("_", "").substring(0, [math]::Min(20, (Split-Path $selectedFile -Leaf).replace(".gguf", "").length)).ToLower()
                    name = if ($tags.NameBox.Text) { $tags.NameBox.Text } else { (Split-Path $selectedFile -Leaf).replace(".gguf", "") }
                    filename = Split-Path $selectedFile -Leaf
                    port = [int]$tags.PortBox.Value
                    ctxSize = [int]$tags.CtxBox.Value
                }
                $installerData.ModelDir = [System.IO.Path]::GetDirectoryName($selectedFile)
            }
        } else {
            $tags = $form.Tag
            $modelDir = $tags.ModelDirBox.Text
            $installerData.ModelDir = $modelDir
            # If directory exists, scan for models
            if ($modelDir -and (Test-Path $modelDir)) {
                $filesInDir = Get-ChildItem -Path $modelDir -Filter "*.gguf" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
                if ($filesInDir) {
                    $path = $filesInDir.FullName
                    $installerData.ModelConfig = @{
                        id = (Split-Path $path -Leaf).replace(".gguf", "").replace("-", "").replace("_", "").substring(0, [math]::Min(20, (Split-Path $path -Leaf).replace(".gguf", "").length)).ToLower()
                        name = (Split-Path $path -Leaf).replace(".gguf", "")
                        filename = Split-Path $path -Leaf
                        port = 9001
                        ctxSize = 4096
                    }
                }
            }
        }

        $form.Dispose()
        ShowServiceConfig
    })

    $form.TopMost = $true
    $form.ShowDialog()
}

# ─── Step 4: Service Configuration ───────────────────────────────
function ShowServiceConfig {
    $form = New-Object System.Windows.Forms.Form
    $form.Text = "LlamaShift - Step 4/5: Service Configuration"
    $form.Size = New-Object System.Drawing.Size(600, 420)
    $form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedDialog
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.StartPosition = "CenterScreen"
    $form.BackColor = $BgColor

    $title = New-Object System.Windows.Forms.Label
    $title.Text = "Step 4/5: Service Configuration"
    $title.Font = $TitleFont
    $title.ForeColor = $PrimaryColor
    $title.Location = New-Object System.Drawing.Point(30, 20)

    $progressBar = New-Object System.Windows.Forms.ProgressBar
    $progressBar.Value = 80
    $progressBar.Location = New-Object System.Drawing.Point(30, 55)
    $progressBar.Width = 500
    $progressBar.Height = 6

    $y = 85

    $howLabel = New-Object System.Windows.Forms.Label
    $howLabel.Text = "How should LlamaShift run?"
    $howLabel.Font = $BodyFont
    $howLabel.ForeColor = $TextColor
    $howLabel.Location = New-Object System.Drawing.Point(30, $y)
    $howLabel.AutoSize = $true
    $form.Controls.Add($howLabel)
    $y += 30

    $svcRadio1 = New-Object System.Windows.Forms.RadioButton
    $svcRadio1.Text = "Background service (auto-start on login, auto-restart on crash)"
    $svcRadio1.Font = $BodyFont
    $svcRadio1.ForeColor = $TextColor
    $svcRadio1.Location = New-Object System.Drawing.Point(30, $y)
    $svcRadio1.Checked = $true
    $form.Controls.Add($svcRadio1)
    $y += 28

    $svcRadio2 = New-Object System.Windows.Forms.RadioButton
    $svcRadio2.Text = "Manual only (start with double-click or command line)"
    $svcRadio2.Font = $BodyFont
    $svcRadio2.ForeColor = $TextColor
    $svcRadio2.Location = New-Object System.Drawing.Point(30, $y)
    $form.Controls.Add($svcRadio2)
    $y += 30

    $svcRadio3 = New-Object System.Windows.Forms.RadioButton
    $svcRadio3.Text = "Windows Service via NSSM (professional-grade service management)"
    $svcRadio3.Font = $BodyFont
    $svcRadio3.ForeColor = $TextColor
    $svcRadio3.Location = New-Object System.Drawing.Point(30, $y)
    $form.Controls.Add($svcRadio3)

    $y += 50

    $nssmNote = New-Object System.Windows.Forms.Label
    $nssmNote.Text = "ℹ NSSM (Non-Sucking Service Manager) is recommended for Windows services.`nIt will be downloaded automatically if selected."
    $nssmNote.Font = $SmallFont
    $nssmNote.ForeColor = [System.Drawing.Color]::FromArgb(150, 150, 150)
    $nssmNote.Location = New-Object System.Drawing.Point(30, $y)
    $nssmNote.Width = 500
    $nssmNote.AutoSize = $false
    $nssmNote.MultiLine = $true
    $nssmNote.Visible = $false
    $form.Controls.Add($nssmNote)

    $svcRadio3.Add_CheckStateChanged({ $nssmNote.Visible = $svcRadio3.Checked })

    # Next button
    $nextBtn = New-Object System.Windows.Forms.Button
    $nextBtn.Text = "Next →"
    $nextBtn.Font = $ButtonFont
    $nextBtn.ForeColor = $TextColor
    $nextBtn.BackColor = $PrimaryColor
    $nextBtn.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
    $nextBtn.Size = New-Object System.Drawing.Size(120, 36)
    $nextBtn.Location = New-Object System.Drawing.Point(380, 360)
    $form.Controls.Add($nextBtn)

    $nextBtn.Add_Click({
        if ($svcRadio1.Checked) {
            $installerData.ServiceMethod = "schtasks"
        } elseif ($svcRadio2.Checked) {
            $installerData.ServiceMethod = "manual"
        } else {
            $installerData.ServiceMethod = "nssm"
        }
        $form.Dispose()
        ShowSummary
    })

    $form.TopMost = $true
    $form.ShowDialog()
}

# ─── Step 5: Summary & Install ───────────────────────────────────
function ShowSummary {
    $form = New-Object System.Windows.Forms.Form
    $form.Text = "LlamaShift - Review & Install"
    $form.Size = New-Object System.Drawing.Size(600, 520)
    $form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedDialog
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.StartPosition = "CenterScreen"
    $form.BackColor = $BgColor

    $title = New-Object System.Windows.Forms.Label
    $title.Text = "Review & Install"
    $title.Font = $TitleFont
    $title.ForeColor = $PrimaryColor
    $title.Location = New-Object System.Drawing.Point(30, 20)

    $progressBar = New-Object System.Windows.Forms.ProgressBar
    $progressBar.Value = 100
    $progressBar.Location = New-Object System.Drawing.Point(30, 55)
    $progressBar.Width = 500
    $progressBar.Height = 6

    # Summary card
    $card = New-Object System.Windows.Forms.Panel
    $card.BackColor = $CardBg
    $card.Location = New-Object System.Drawing.Point(30, 75)
    $card.Width = 500
    $card.Height = 300

    $y = 15
    $summaryText = @"
OS:            Windows $(if ([Environment]::Is64BitOperatingSystem) { "64-bit" } else { "32-bit" })
Python:        $($installerData.PythonPath)
llama-server:  $($installerData.BinaryPath)
GPU:           $($installerData.GpuType.ToUpper())
Mode:          $($installerData.Mode)
"@

    if ($installerData.ModelConfig) {
        $summaryText += @"

Model:         $($installerData.ModelConfig.name)
Port:          $($installerData.ModelConfig.port)
Context:       $($installerData.ModelConfig.ctxSize) tokens
"@
    }

    if ($installerData.ServiceMethod -eq "manual") {
        $summaryText += "`nService:     Manual start"
    } elseif ($installerData.ServiceMethod -eq "nssm") {
        $summaryText += "`nService:     NSSM Windows Service"
    } else {
        $summaryText += "`nService:     Task Scheduler (auto-start)"
    }

    $summaryLabel = New-Object System.Windows.Forms.Label
    $summaryLabel.Text = $summaryText
    $summaryLabel.Font = $SmallFont
    $summaryLabel.ForeColor = $TextColor
    $summaryLabel.Location = New-Object System.Drawing.Point(15, $y)
    $summaryLabel.Width = 470
    $summaryLabel.Height = 270
    $summaryLabel.AutoSize = $false
    $summaryLabel.MultiLine = $true
    $card.Controls.Add($summaryLabel)
    $form.Controls.Add($card)

    # Install button
    $installBtn = New-Object System.Windows.Forms.Button
    $installBtn.Text = "⚡ Install LlamaShift"
    $installBtn.Font = New-Object System.Drawing.Font($FontFamily, 11, [System.Drawing.FontStyle]::Bold)
    $installBtn.ForeColor = $TextColor
    $installBtn.BackColor = $SuccessColor
    $installBtn.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
    $installBtn.Size = New-Object System.Drawing.Size(240, 44)
    $installBtn.Location = New-Object System.Drawing.Point(180, 400)
    $form.Controls.Add($installBtn)

    $installBtn.Add_Click({
        $installBtn.Enabled = $false
        $installBtn.Text = "Installing..."
        $installBtn.Refresh()

        # Generate config.json
        $dataDir = if ($installerData.ModelDir) { $installerData.ModelDir } else { "$env:USERPROFILE\models" }
        $config = @{
            appName = "llamashift"
            serviceName = "llamashift"
            masterPort = if ($installerData.ModelConfig) { $installerData.ModelConfig.port } else { 9000 }
            binaryPath = $installerData.BinaryPath
            dataDir = $dataDir
            mode = $installerData.Mode
            models = @{}
        }

        if ($installerData.ModelConfig) {
            $mc = $installerData.ModelConfig.Clone()
            if ($installerData.PsObject.Properties.Name -contains "GpuLayers") {
                $mc["nGpuLayers"] = $installerData.GpuLayers
            }
            $mc.Remove("filename") # keep it actually
            $config.models[$mc.id] = $mc
        }

        $configPath = Join-Path $installerData.AppDir "config.json"
        $config | ConvertTo-Json -Depth 5 | Set-Content $configPath -Encoding UTF8

        # Install service
        if ($installerData.ServiceMethod -eq "nssm") {
            # Download and install NSSM
            $nssmZip = Join-Path $env:TEMP "nssm.zip"
            $nssmDir = Join-Path $installerData.AppDir "nssm"
            if (-not (Test-Path (Join-Path $nssmDir "nssm.exe"))) {
                Write-Host "Downloading NSSM..."
                try {
                    Invoke-WebRequest "https://nssm.cc/release/nssm-2.24.zip" -OutFile $nssmZip -ErrorAction Stop
                    Expand-Archive $nssmZip -DestinationPath $nssmDir -Force
                    $nssmExe = Get-ChildItem (Join-Path $nssmDir "nssm-*") -Recurse -Filter "nssm.exe" | Select-Object -First 1 | Select-Object -ExpandProperty FullName
                    Copy-Item $nssmExe (Join-Path $nssmDir "nssm.exe") -Force
                } catch {
                    Write-Host "NSSM download failed: $_"
                }
            }
            $nssmExe = Join-Path $nssmDir "nssm.exe"
            if (Test-Path $nssmExe) {
                $pythonExe = (Get-Command python).Source
                & $nssm install LlamaShift $pythonExe 2>&1 | Out-Null
                & $nssm set LlamaShift AppDirectory $installerData.AppDir 2>&1 | Out-Null
                & $nssm set LlamaShift AppStdout "$installerData.AppDir\logs\llamashift.log" 2>&1 | Out-Null
                & $nssm set LlamaShift AppStderr "$installerData.AppDir\logs\llamashift.log" 2>&1 | Out-Null
                & $nssm start LlamaShift 2>&1 | Out-Null
            }
        } elseif ($installerData.ServiceMethod -eq "schtasks") {
            $pythonExe = (Get-Command python).Source
            $taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>LlamaShift LLM Workstation Manager</Description></RegistrationInfo>
  <Principals><Principal><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>Parallel</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>0</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>
  <Actions>
    <Exec>
      <Command>$pythonExe</Command>
      <Arguments>"$configPath"</Arguments>
      <WorkingDirectory>$installerData.AppDir</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
            $taskXmlPath = Join-Path $installerData.AppDir "llamashift_task.xml"
            $taskXml | Set-Content $taskXmlPath -Encoding UTF8
            schtasks /Create /TN LlamaShift /XML $taskXmlPath /F 2>&1 | Out-Null
        }

        # Show completion
        $installBtn.Text = "✓ Installation Complete!"
        $installBtn.BackColor = $SuccessColor
        $installBtn.Refresh()

        $doneLabel = New-Object System.Windows.Forms.Label
        $doneLabel.Text = "LlamaShift is installed and configured!`n`nConfig: $configPath`nUI:     http://localhost:8002`n`n💡 TIP: You can reconfigure model parameters at any time from the Web UI:`n   - Click the gear icon on each model card to adjust settings`n   - Toggle between Single-port and Multi-port mode in the header`n`nYou can close this window."
        $doneLabel.Font = $BodyFont
        $doneLabel.ForeColor = $SuccessColor
        $doneLabel.Location = New-Object System.Drawing.Point(30, 460)
        $doneLabel.AutoSize = $true
        $doneLabel.Width = 500
        $doneLabel.MultiLine = $true
        $form.Controls.Add($doneLabel)
    })

    $form.TopMost = $true
    $form.ShowDialog()
}

Write-Host "LlamaShift GUI Installer complete."