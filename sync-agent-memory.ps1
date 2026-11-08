param(
    [string]$Snippet = (Join-Path $env:USERPROFILE '.workbuddy\agent-memory-snippet.md'),
    [switch]$DryRun,
    [switch]$Update,
    [switch]$Check,
    [switch]$Status,
    [switch]$Lint,
    [string[]]$ExtraTargets = @()
)
# Pure ASCII. Idempotent multi-block distributor.
# Snippet holds one or more blocks: <!-- name --> ... <!-- /name -->
# Every block is synced independently into every target.
# Usage:
#   powershell -NoProfile -File sync-agent-memory.ps1 -Status          # read-only friendly summary
#   powershell -NoProfile -File sync-agent-memory.ps1 -Check           # read-only; exit 1 if any drift (CI gate)
#   powershell -NoProfile -File sync-agent-memory.ps1 -Lint            # read-only; lint source blocks (length/secret/dup)
#   powershell -NoProfile -File sync-agent-memory.ps1 -DryRun -Update  # read-only preview of writes
#   powershell -NoProfile -File sync-agent-memory.ps1 -Update          # actually sync (auto .bak per changed file)
#   powershell -NoProfile -File sync-agent-memory.ps1 -Update -ExtraTargets "D:\team\AGENTS.md"
$ErrorActionPreference = 'Stop'
# Check/Status/Lint are read-only; Check+Status also force full diff (Update) so content is compared.
if ($Check -or $Status) { $DryRun = $true; $Update = $true }
$u8 = New-Object System.Text.UTF8Encoding($false)
$snippetText = [System.IO.File]::ReadAllText($Snippet, (New-Object System.Text.UTF8Encoding($false, $true)))
$snippetText = $snippetText.Replace("`r`n", "`n")

# --- parse blocks from snippet ---
$blocks = New-Object System.Collections.ArrayList
$curLines = $null
$curName = ''
foreach ($raw in ($snippetText -split "`n")) {
    $line = $raw.TrimEnd([char]13)
    $trimmed = $line.Trim()
    if ($trimmed -match '^<!-- ([a-z0-9-]+) -->$') {
        $curName = $Matches[1]
        $curLines = New-Object System.Collections.ArrayList
        [void]$curLines.Add($line)
        continue
    }
    if ($null -ne $curLines) {
        [void]$curLines.Add($line)
        if ($trimmed -eq ('<!-- /' + $curName + ' -->')) {
            $body = ($curLines -join "`n").Trim()
            $o = New-Object PSObject
            Add-Member -InputObject $o -NotePropertyName Name -NotePropertyValue $curName
            Add-Member -InputObject $o -NotePropertyName Start -NotePropertyValue ('<!-- ' + $curName + ' -->')
            Add-Member -InputObject $o -NotePropertyName End -NotePropertyValue ('<!-- /' + $curName + ' -->')
            Add-Member -InputObject $o -NotePropertyName Body -NotePropertyValue $body
            [void]$blocks.Add($o)
            $curLines = $null
        }
    }
}
if ($blocks.Count -eq 0) { throw ('No blocks found in snippet: ' + $Snippet) }

# --- Lint source blocks (read-only, exits early) ---
if ($Lint) {
    $issues = 0
    $keyPat = 'sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|api[_-]?key\s*[:=]|token\s*[:=]|password\s*[:=]|secret\s*[:=]|Bearer\s+[A-Za-z0-9._-]+'
    foreach ($b in $blocks) {
        $wc = $b.Body.Length
        if ($wc -gt 3000) { Write-Output ("LINT_WARN=" + $b.Name + " too_long chars=" + $wc); $issues++ }
        if ($b.Body -match $keyPat) { Write-Output ("LINT_WARN=" + $b.Name + " possible_secret"); $issues++ }
        $lines = $b.Body -split "`n" | Where-Object { $_.Trim() -ne '' }
        $dups = $lines | Group-Object | Where-Object { $_.Count -gt 1 }
        if ($dups) { Write-Output ("LINT_WARN=" + $b.Name + " duplicate_lines=" + ($dups.Name -join '|')); $issues++ }
    }
    Write-Output ("LINT_ISSUES=" + $issues)
    if ($issues -gt 0) { exit 1 } else { Write-Output "LINT_OK"; exit 0 }
}

$drift = New-Object System.Collections.ArrayList

$h = $env:USERPROFILE
$targets = @(
    (Join-Path $h '.workbuddy\AGENTS.md'),
    (Join-Path $h '.claude\CLAUDE.md'),
    (Join-Path $h '.codex\AGENTS.md'),
    (Join-Path $h '.gemini\GEMINI.md'),
    (Join-Path $h '.cursor\rules\win-encoding.mdc'),
    (Join-Path $h '.cursorrules'),
    (Join-Path $h 'AGENTS.md'),
    (Join-Path $h '.config\opencode\AGENTS.md'),
    (Join-Path $h '.copilot\AGENTS.md'),
    (Join-Path $h '.qoder\AGENTS.md'),
    (Join-Path $h '.qoder-cn\AGENTS.md'),
    (Join-Path $h '.trae\user_rules\win-encoding.md'),
    (Join-Path $h '.trae-cn\user_rules\win-encoding.md'),
    (Join-Path $h '.trae-solo\AGENTS.md'),
    (Join-Path $h '.trae-solo\user_rules\win-encoding.md'),
    (Join-Path $h '.dsh\AGENTS.md')
) + $ExtraTargets

foreach ($t in $targets) {
    $dir = Split-Path -Parent $t
    if (-not (Test-Path -LiteralPath $t)) {
        foreach ($b in $blocks) { [void]$drift.Add(("CREATE " + $t + " " + $b.Name)) }
        if ($DryRun) { Write-Output ("WOULD_CREATE=" + $t); continue }
        if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        [System.IO.File]::WriteAllText($t, $snippetText.Trim() + "`n", $u8)
        Write-Output ("CREATED=" + $t)
        continue
    }
    $full = [System.IO.File]::ReadAllText($t, $u8)
    $full = $full.Replace("`r`n", "`n")
    $work = $full
    $changed = $false
    foreach ($b in $blocks) {
        $start = $work.IndexOf($b.Start)
        if ($start -ge 0) {
            $end = $work.IndexOf($b.End, $start)
            if ($end -lt 0) {
                Write-Output ("SKIP_NO_END=" + $t + " BLOCK=" + $b.Name)
                [void]$drift.Add(("NO_END " + $t + " " + $b.Name))
                continue
            }
            if (-not $Update) {
                Write-Output ("SKIP_ALREADY=" + $t + " BLOCK=" + $b.Name)
                continue
            }
            $blockEnd = $end + $b.End.Length
            $oldBlock = $work.Substring($start, $blockEnd - $start)
            if ($oldBlock.Trim() -eq $b.Body) {
                Write-Output ("SKIP_SAME=" + $t + " BLOCK=" + $b.Name)
                continue
            }
            if ($DryRun) {
                Write-Output ("WOULD_UPDATE=" + $t + " BLOCK=" + $b.Name)
                [void]$drift.Add(("UPDATE " + $t + " " + $b.Name))
                $changed = $true
                continue
            }
            $work = $work.Substring(0, $start) + $b.Body + $work.Substring($blockEnd)
            $changed = $true
            Write-Output ("UPDATED=" + $t + " BLOCK=" + $b.Name)
        } else {
            if ($DryRun) {
                Write-Output ("WOULD_APPEND=" + $t + " BLOCK=" + $b.Name)
                [void]$drift.Add(("APPEND " + $t + " " + $b.Name))
                $changed = $true
                continue
            }
            $work = $work.TrimEnd() + "`n`n" + $b.Body + "`n"
            $changed = $true
            Write-Output ("APPENDED=" + $t + " BLOCK=" + $b.Name)
        }
    }
    if ($changed -and -not $DryRun) {
        $bak = $t + '.bak-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
        [System.IO.File]::Copy($t, $bak, $true)
        [System.IO.File]::WriteAllText($t, $work, $u8)
        Write-Output ("BACKUP=" + $bak)
    }
}

# --- summary modes ---
$total = $targets.Count * $blocks.Count
$consistent = $total - $drift.Count
if ($Status) {
    Write-Output "=== SYNC STATUS ==="
    Write-Output ("SNIPPET=" + $Snippet)
    Write-Output ("TARGETS=" + $targets.Count + " BLOCKS=" + $blocks.Count + " CHECKPOINTS=" + $total)
    Write-Output ("CONSISTENT=" + $consistent + " DRIFT=" + $drift.Count)
    if ($drift.Count -gt 0) {
        Write-Output "DRIFT_DETAIL:"
        foreach ($d in $drift) { Write-Output ("  " + $d) }
    } else {
        Write-Output "ALL_CONSISTENT"
    }
}
if ($Check) {
    Write-Output ("DRIFT=" + $drift.Count)
    if ($drift.Count -gt 0) { exit 1 } else { exit 0 }
}
