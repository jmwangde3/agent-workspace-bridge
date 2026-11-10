param(
    [string]$Snippet = (Join-Path $env:USERPROFILE '.workbuddy\agent-memory-snippet.md'),
    [switch]$DryRun,
    [switch]$Update,
    [switch]$Check,
    [switch]$Status,
    [switch]$Lint,
    [string[]]$ExtraTargets = @(),
    [switch]$TargetsOnly
)
$ErrorActionPreference = 'Stop'
$operationCount = @($Status, $Check, $Lint, $Update | Where-Object { $_ }).Count
if ($operationCount -gt 1) {
    Write-Output 'RULE_ERROR=Choose only one of -Status, -Check, -Lint, -Update'
    exit 2
}
if (!$operationCount -and !$DryRun) { $Status = $true }
$allowWrite = $Update -and !$DryRun
$u8 = [System.Text.UTF8Encoding]::new($false, $true)
$marker = [regex]'<!--[ \t]*(?<end>/)?(?<name>[a-z0-9-]+)[ \t]*-->'

function Get-Signature([byte[]]$Bytes) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return [Convert]::ToBase64String($sha.ComputeHash($Bytes)) }
    finally { $sha.Dispose() }
}

function Read-RuleText([string]$Path) {
    $bytes = [IO.File]::ReadAllBytes($Path)
    $encoding = $u8
    $offset = 0
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 239 -and $bytes[1] -eq 187 -and $bytes[2] -eq 191) {
        $encoding = [Text.UTF8Encoding]::new($true, $true)
        $offset = 3
    } elseif ($bytes.Length -ge 2) {
        if ($bytes[0] -eq 255 -and $bytes[1] -eq 254) {
            $encoding = [Text.UnicodeEncoding]::new($false, $true, $true)
            $offset = 2
        } elseif ($bytes[0] -eq 254 -and $bytes[1] -eq 255) {
            $encoding = [Text.UnicodeEncoding]::new($true, $true, $true)
            $offset = 2
        }
    }
    $text = $encoding.GetString($bytes, $offset, $bytes.Length - $offset)
    if ($text.Contains([string][char]0)) { throw 'Unsupported text encoding or embedded NUL' }
    $eol = [regex]::Match($text, '\r\n|\n|\r').Value
    if (!$eol) { $eol = [Environment]::NewLine }
    return @{ Text = $text; Encoding = $encoding; Eol = $eol; Signature = (Get-Signature $bytes) }
}

function Parse-ManagedBlocks([string]$Text, [hashtable]$Known) {
    $blocks = [ordered]@{}
    $current = $null
    $number = 0
    foreach ($line in [regex]::Matches($Text, '[^\r\n]*(?:\r\n|\n|\r|$)')) {
        $number++
        foreach ($hit in $marker.Matches($line.Value)) {
            $name = $hit.Groups['name'].Value
            if ($null -ne $Known -and !$Known.ContainsKey($name)) { continue }
            if ($hit.Value -cne $line.Value.Trim()) { throw "Marker must occupy its own line at line $number ($name)" }
            if ($hit.Groups['end'].Success) {
                if (!$current -or $current.Name -cne $name) { throw "Unexpected closing marker at line $number ($name)" }
                $end = $line.Index + $line.Value.TrimEnd([char]13, [char]10).Length
                $blocks.Add($name, @{ Start = $current.Start; End = $end; Text = $Text.Substring($current.Start, $end - $current.Start) })
                $current = $null
            } else {
                if ($current) { throw "Nested marker at line $number ($name)" }
                if ($blocks.Contains($name)) { throw "Duplicate block at line $number ($name)" }
                $current = @{ Name = $name; Start = $line.Index }
            }
        }
    }
    if ($current) { throw ('Unclosed block: ' + $current.Name) }
    return $blocks
}

function Render-ManagedBlocks($Record, $Blocks, [hashtable]$Known, [bool]$ReplaceExisting) {
    $existing = Parse-ManagedBlocks $Record.Text $Known
    $edits = @()
    $append = @()
    $changes = @()
    foreach ($name in $Blocks.Keys) {
        $body = [regex]::Replace($Blocks[$name].Text, '\r\n|\n|\r', $Record.Eol)
        $old = $existing[$name]
        if (!$old) {
            $append += $body
            $changes += "APPEND $name"
        } elseif ($ReplaceExisting -and [regex]::Replace($old.Text, '\r\n|\n|\r', $Record.Eol) -cne $body) {
            $edits += @{ Start = $old.Start; End = $old.End; Text = $body }
            $changes += "UPDATE $name"
        }
    }
    $work = $Record.Text
    foreach ($edit in ($edits | Sort-Object { $_.Start } -Descending)) {
        $work = $work.Substring(0, $edit.Start) + $edit.Text + $work.Substring($edit.End)
    }
    if ($append.Count) {
        if ($work.Length -and $work -notmatch '(?:\r\n|\n|\r)\z') { $work += $Record.Eol }
        if ($work.Length) { $work += $Record.Eol }
        $work += ($append -join ($Record.Eol + $Record.Eol)) + $Record.Eol
    }
    $null = Parse-ManagedBlocks $work $Known
    return @{ Text = $work; Changes = $changes }
}

function Write-RuleText([string]$Path, [string]$Text, $Record) {
    $folder = [IO.Path]::GetDirectoryName($Path)
    [void][IO.Directory]::CreateDirectory($folder)
    $suffix = [guid]::NewGuid().ToString('N')
    $temp = [IO.Path]::Combine($folder, '.awb-rule-' + $suffix + '.tmp')
    $encoding = $u8
    if ($Record) { $encoding = $Record.Encoding }
    $stream = [IO.File]::Open($temp, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $bom = $encoding.GetPreamble()
        $body = $encoding.GetBytes($Text)
        $stream.Write($bom, 0, $bom.Length)
        $stream.Write($body, 0, $body.Length)
        $stream.Flush($true)
    } finally { $stream.Dispose() }
    try {
        $written = Read-RuleText $temp
        if ($written.Text -cne $Text) { throw 'Temporary file read-back did not match' }
        if ($Record) {
            if ([IO.File]::GetAttributes($Path) -band [IO.FileAttributes]::ReparsePoint) { throw 'Target is a link' }
            if ($env:OS -eq 'Windows_NT') {
                $names = @(& fsutil.exe hardlink list $Path 2>$null)
                if ($LASTEXITCODE -ne 0 -or $names.Count -ne 1) { throw 'Cannot safely replace a hard-linked or unverified target' }
            }
            if ((Get-Signature ([IO.File]::ReadAllBytes($Path))) -cne $Record.Signature) {
                throw 'Target changed during planning'
            }
            $backup = $Path + '.bak-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + $suffix
            [IO.File]::Replace($temp, $Path, $backup)
            Write-Output ('BACKUP=' + $backup)
            if ((Get-Signature ([IO.File]::ReadAllBytes($backup))) -cne $Record.Signature) {
                throw 'Target changed during replacement; concurrent contents are preserved in the backup'
            }
        } else {
            [IO.File]::Move($temp, $Path)
        }
        if ((Get-Signature ([IO.File]::ReadAllBytes($Path))) -cne $written.Signature) {
            throw 'Target read-back did not match the staged file'
        }
    } catch {
        if ([IO.File]::Exists($temp)) { Write-Output ('STAGED_FILE=' + $temp) }
        throw
    }
}

try {
    $source = Read-RuleText $Snippet
    $blocks = Parse-ManagedBlocks $source.Text $null
    if (!$blocks.Count) { throw 'No managed blocks in snippet' }
} catch {
    Write-Output ('RULE_ERROR=' + $Snippet + ' ' + $_.Exception.Message)
    exit 2
}
$known = @{}
foreach ($name in $blocks.Keys) { $known[$name] = $true }

if ($Lint) {
    $issues = 0
    $keyPat = 'sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|api[_-]?key\s*[:=]|token\s*[:=]|password\s*[:=]|secret\s*[:=]|Bearer\s+[A-Za-z0-9._-]+'
    foreach ($name in $blocks.Keys) {
        $body = $blocks[$name].Text
        if ($body.Length -gt 3000) { Write-Output "LINT_WARN=$name too_long"; $issues++ }
        if ($body -match $keyPat) { Write-Output "LINT_WARN=$name possible_secret"; $issues++ }
        $dups = $body -split '\r\n|\n|\r' | Where-Object { $_.Trim() } | Group-Object | Where-Object { $_.Count -gt 1 }
        if ($dups) { Write-Output "LINT_WARN=$name duplicate_lines"; $issues++ }
    }
    Write-Output "LINT_ISSUES=$issues"
    if ($issues) { exit 1 }
    Write-Output 'LINT_OK'
    exit 0
}

$relative = @(
    '.workbuddy\AGENTS.md', '.claude\CLAUDE.md', '.codex\AGENTS.md', '.gemini\GEMINI.md',
    '.cursor\rules\win-encoding.mdc', '.cursorrules', 'AGENTS.md', '.config\opencode\AGENTS.md',
    '.copilot\AGENTS.md', '.qoder\AGENTS.md', '.qoder-cn\AGENTS.md', '.trae\user_rules\win-encoding.md',
    '.trae-cn\user_rules\win-encoding.md', '.trae-solo\AGENTS.md', '.trae-solo\user_rules\win-encoding.md',
    '.dsh\AGENTS.md'
)
$targets = @($ExtraTargets)
if (!$TargetsOnly) { $targets = @($relative | ForEach-Object { Join-Path $env:USERPROFILE $_ }) + $targets }
try {
    $targets = @($targets | ForEach-Object { [IO.Path]::GetFullPath($_) } | Select-Object -Unique)
} catch {
    Write-Output ('RULE_ERROR=' + $_.Exception.Message)
    exit 2
}
if (!$targets.Count) { Write-Output 'RULE_ERROR=No targets'; exit 2 }
$drift = 0
$errors = 0
foreach ($target in $targets) {
    try {
        if (!(Test-Path -LiteralPath $target)) {
            $drift += $blocks.Count
            if (!$allowWrite) { Write-Output ('WOULD_CREATE=' + $target); continue }
            Write-RuleText $target ($source.Text.Trim() + $source.Eol) $null
            Write-Output ('CREATED=' + $target)
            continue
        }
        if ([IO.File]::GetAttributes($target) -band [IO.FileAttributes]::ReparsePoint) { throw 'Target is a link' }
        $record = Read-RuleText $target
        $plan = Render-ManagedBlocks $record $blocks $known $true
        $drift += $plan.Changes.Count
        if (!$plan.Changes.Count) { continue }
        if ($allowWrite) { Write-RuleText $target $plan.Text $record }
        foreach ($change in $plan.Changes) {
            $verb, $name = $change -split ' ', 2
            $label = @{ APPEND = 'APPENDED'; UPDATE = 'UPDATED' }[$verb]
            if (!$allowWrite) { $label = 'WOULD_' + $verb }
            Write-Output ($label + '=' + $target + ' BLOCK=' + $name)
        }
    } catch {
        $errors++
        Write-Output ('RULE_ERROR=' + $target + ' ' + $_.Exception.Message)
    }
}
if ($Status) {
    $total = $targets.Count * $blocks.Count
    Write-Output ('TARGETS=' + $targets.Count + ' BLOCKS=' + $blocks.Count + ' CHECKPOINTS=' + $total)
    if (!$errors) { Write-Output ('CONSISTENT=' + ($total - $drift)) }
}
if ($Check -or $Status) { Write-Output "DRIFT=$drift" }
if ($errors) { Write-Output "RULE_ERRORS=$errors"; exit 2 }
if ($Check -and $drift) { exit 1 }
exit 0
