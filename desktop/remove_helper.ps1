param([Parameter(Mandatory=$true)][string]$RequestPath)
$ErrorActionPreference = 'Stop'
$request = $null
$result = @{status='failed'; removed_program=@(); recycled_data=@(); preserved=@(); errors=@(); recovery_zip=$null}

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class RemovalProcess {
  [DllImport("kernel32", SetLastError=true)] public static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
  [DllImport("kernel32", SetLastError=true)] public static extern bool GetProcessTimes(IntPtr h, out long creation, out long exit, out long kernel, out long user);
  [DllImport("kernel32", SetLastError=true)] public static extern uint WaitForSingleObject(IntPtr h, uint milliseconds);
  [DllImport("kernel32")] public static extern bool CloseHandle(IntPtr h);
}
'@

function Assert-NoLink([string]$Path) {
    $cursor = [System.IO.Path]::GetFullPath($Path)
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "路径是链接或junction，保留：$cursor" }
        }
        $parent = [System.IO.Directory]::GetParent($cursor)
        if ($null -eq $parent) { break }
        $cursor = $parent.FullName
    }
}

function Owned-Path([string]$Root, [string]$Relative) {
    if ($Relative -match '[:\\]' -or $Relative.StartsWith('/') -or $Relative -match '(^|/)(\.|\.\.)(/|$)' -or $Relative -match '//') { throw '无效清单路径' }
    $full = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($Root, $Relative.Replace('/', '\')))
    if (-not $full.StartsWith($Root.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw '清单路径超出程序目录' }
    Assert-NoLink $full
    return $full
}

function Matches([string]$Path, $Row) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.Length -ne $Row.size) { return $false }
    $stream = [System.IO.File]::OpenRead($Path)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { $hash = [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-','').ToLowerInvariant() }
    finally { $stream.Dispose(); $sha.Dispose() }
    return $hash -eq $Row.sha256
}

function Profile-Snapshot([string]$Directory, [string]$DataRoot) {
    Assert-NoLink $Directory
    foreach ($item in Get-ChildItem -LiteralPath $Directory -Force) {
        Assert-NoLink $item.FullName
        if ($item.PSIsContainer) { Profile-Snapshot $item.FullName $DataRoot }
        else {
            $stream = [System.IO.File]::OpenRead($item.FullName)
            $sha = [System.Security.Cryptography.SHA256]::Create()
            try { $hash = [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-','').ToLowerInvariant() }
            finally { $stream.Dispose(); $sha.Dispose() }
            @{relative=$item.FullName.Substring($DataRoot.Length+1).Replace('\','/'); size=$item.Length; sha256=$hash}
        }
    }
}

function Remove-EmptyParents([string]$Path, [string]$Root) {
    $directory = [System.IO.Path]::GetDirectoryName($Path)
    while ($directory.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase)) {
        if (-not (Test-Path -LiteralPath $directory -PathType Container)) { break }
        Assert-NoLink $directory
        if (@(Get-ChildItem -LiteralPath $directory -Force).Count -ne 0) { break }
        [System.IO.Directory]::Delete($directory, $false)
        if ($directory -eq $Root) { break }
        $directory = [System.IO.Path]::GetDirectoryName($directory)
    }
}

$handle = [IntPtr]::Zero
$dataLock = $null
try {
    $request = Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($request.schema -ne 1 -or $request.app_id -ne 'contract-review-desktop') { throw '请求不属于合同审查台' }
    $root = [System.IO.Path]::GetFullPath($request.app_dir).TrimEnd('\')
    $data = [System.IO.Path]::GetFullPath($request.data_dir).TrimEnd('\')
    Assert-NoLink $root
    Assert-NoLink $data
    if ($root -eq [System.IO.Path]::GetPathRoot($root).TrimEnd('\') -or
        $root -eq [Environment]::GetFolderPath('Desktop') -or $root -eq $env:USERPROFILE -or
        (Test-Path -LiteralPath (Join-Path $root '.git'))) { throw '不移除盘根、桌面、个人目录或仓库' }
    if ($request.delete_data -and $data -ne (Join-Path $root '我的数据')) { throw '不能删除自定义外部数据目录' }
    # Open while the requesting process is alive. The creation FILETIME prevents
    # PID reuse; retaining the handle waits for this exact process, not a number.
    $handle = [RemovalProcess]::OpenProcess(0x101000, $false, [int]$request.pid)
    if ($handle -eq [IntPtr]::Zero) { throw '无法取得请求进程句柄，未移除文件' }
    [long]$created=0; [long]$ended=0; [long]$kernel=0; [long]$userTime=0
    if (-not [RemovalProcess]::GetProcessTimes($handle,[ref]$created,[ref]$ended,[ref]$kernel,[ref]$userTime) -or
        $created.ToString() -ne $request.creation_filetime) { throw '请求进程身份已变化，未移除文件' }
    [System.IO.File]::WriteAllText($request.ready_path, 'ready')
    if ([RemovalProcess]::WaitForSingleObject($handle, 600000) -ne 0) { throw '等待程序退出超时，未移除文件' }
    [RemovalProcess]::CloseHandle($handle) | Out-Null
    $handle = [IntPtr]::Zero
    # Another copy may have started after the original process exited. Use the
    # same OS byte lock, rather than trusting a stale PID or deleting its data.
    $lockPath = Join-Path $data '.desktop.lock'
    if (Test-Path -LiteralPath $lockPath -PathType Leaf) {
        Assert-NoLink $lockPath
        $dataLock = [System.IO.FileStream]::new($lockPath, [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::ReadWrite, ([System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete))
        $dataLock.Lock(0, 1)
    }
    # Recheck every finite target after exit. Never remove a directory tree.
    $result.preserved = @($request.preserved_files)
    if ($request.delete_data) {
        Add-Type -AssemblyName System.IO.Compression
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        Add-Type -AssemblyName Microsoft.VisualBasic
        $ownedData = @()
        $dataRows = @($request.data_files | Where-Object { -not $_.relative.StartsWith('webview2/') })
        # WebView2 flushes its own cache on shutdown. Its reserved namespace is
        # resnapshotted only after the exact owning process has actually exited.
        foreach ($directory in $request.refresh_data_dirs) {
            if ($directory -ne 'webview2') { throw '无效的缓存目录' }
            $profile = Owned-Path $data 'webview2'
            if (Test-Path -LiteralPath $profile -PathType Container) { $dataRows += @(Profile-Snapshot $profile $data) }
        }
        foreach ($row in $dataRows) {
            if ($row.relative -notmatch '^(runtime/(reader-state|review-jobs|demo-contracts)/|webview2/|runtime/demo-seed\.json$|\.desktop-initialized$)') { throw '请求含非受管数据' }
            $path = Owned-Path $data $row.relative
            if (-not (Test-Path -LiteralPath $path)) { continue }
            if (-not (Matches $path $row)) { $result.preserved += "$path（准备后发生变化）"; continue }
            $ownedData += @{path=$path; row=$row}
        }
        if ($ownedData.Count -gt 0) {
            # A temporary verified recovery ZIP covers Windows configurations
            # which silently bypass their Recycle Bin. Remove it only after
            # every deleted file is verified in the actual Bin by origin/hash.
            Assert-NoLink $request.recovery_dir
            New-Item -ItemType Directory -Path $request.recovery_dir -Force | Out-Null
            $zipPath = Join-Path $request.recovery_dir '我的数据.zip'
            $result.recovery_zip = $zipPath
            $zip = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
            try {
                foreach ($entry in $ownedData) { [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip,$entry.path,$entry.row.relative) | Out-Null }
            } finally { $zip.Dispose() }
            $verify = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
            try {
                foreach ($entry in $ownedData) {
                    $member = $verify.GetEntry($entry.row.relative)
                    $stream = $member.Open()
                    $sha = [System.Security.Cryptography.SHA256]::Create()
                    try { $hash = [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-','').ToLowerInvariant() }
                    finally { $stream.Dispose(); $sha.Dispose() }
                    if ($member.Length -ne $entry.row.size -or $hash -ne $entry.row.sha256) { throw '临时数据恢复包校验失败，数据尚未删除' }
                }
            } finally { $verify.Dispose() }
            $deleteStarted = Get-Date
            $shell = New-Object -ComObject Shell.Application
            $bin = $shell.Namespace(10)
            $pending = @{}
            foreach ($entry in $ownedData) {
                $path = Owned-Path $data $entry.row.relative
                if (-not (Matches $path $entry.row)) { $result.preserved += "$path（移除前变化）"; continue }
                [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($path,
                    [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
                    [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin,
                    [Microsoft.VisualBasic.FileIO.UICancelOption]::ThrowException)
                if (Test-Path -LiteralPath $path) { throw "回收未完成：$path" }
                $pending[$path] = $entry.row
            }
            # Scan the actual Bin once, not once per record/cache file. Shell
            # metadata is nonlocalized; require origin, time and recycled hash.
            foreach ($item in $bin.Items()) {
                $origin = [string]$item.ExtendedProperty('System.Recycle.DeletedFrom')
                $originalName = [string]$item.ExtendedProperty('System.FileName')
                if (-not $origin -or -not $originalName) { continue }
                $original = [System.IO.Path]::Combine($origin, $originalName)
                if (-not $pending.ContainsKey($original)) { continue }
                $date = $item.ExtendedProperty('System.Recycle.DateDeleted')
                if ($null -ne $date -and [datetime]$date -ge $deleteStarted.ToUniversalTime().AddSeconds(-2) -and
                    [datetime]$date -le (Get-Date).ToUniversalTime().AddSeconds(2) -and
                    (Matches $item.Path $pending[$original])) {
                    $pending.Remove($original)
                    $result.recycled_data += $original
                }
            }
            if ($pending.Count -gt 0) { throw "不能核实回收站恢复项，已保留恢复ZIP：$($pending.Keys -join '；')" }
            foreach ($path in $result.recycled_data) { Remove-EmptyParents $path $data }
            # Every recycled item has an actual restorable counterpart. No
            # second permanent copy of records/settings remains behind.
            [System.IO.File]::Delete($zipPath)
            [System.IO.Directory]::Delete($request.recovery_dir, $false)
            $result.recovery_zip = $null
        }
    }
    foreach ($row in $request.program_files) {
        try {
            $path = Owned-Path $root $row.relative
            if (-not (Test-Path -LiteralPath $path)) { continue }
            if (-not (Matches $path $row)) { $result.preserved += "$path（准备后发生变化）"; continue }
            [System.IO.File]::Delete($path)
            $result.removed_program += $path
            Remove-EmptyParents $path $root
        } catch { $result.errors += $_.Exception.Message }
    }
    if ($request.delete_data) {
        # Keep the coordination path AND its locked handle valid until every
        # program-file operation has finished. Unlinking it earlier would let
        # another instance create a different file and bypass the held lock.
        foreach ($relative in @('.desktop-instance.json', '.desktop.lock')) {
            $path = Owned-Path $data $relative
            if (Test-Path -LiteralPath $path -PathType Leaf) {
                [System.IO.File]::Delete($path)
                Remove-EmptyParents $path $data
            }
        }
    }
    if ($result.errors.Count -gt 0) { $result.status='partial' }
    elseif ($result.preserved.Count -gt 0) { $result.status='partial' }
    else { $result.status='complete' }
} catch {
    $result.errors += $_.Exception.Message
} finally {
    if ($null -ne $dataLock) { $dataLock.Dispose() }
    if ($handle -ne [IntPtr]::Zero) { [RemovalProcess]::CloseHandle($handle) | Out-Null }
    if ($null -ne $request) {
        $result.data_dir = $request.data_dir
        $result.delete_data = $request.delete_data
        $reportTemporary = $request.report_path + '.tmp'
        [System.IO.File]::WriteAllText($reportTemporary, ($result | ConvertTo-Json -Depth 8),
            [System.Text.UTF8Encoding]::new($true))
        [System.IO.File]::Move($reportTemporary, $request.report_path)
        if ($request.show_result) {
            try {
                Add-Type -AssemblyName PresentationFramework
                $message = if ($result.status -eq 'complete') { '软件移除完成。' } else { '移除未全部完成；未确认的文件保留原位。' }
                if (-not $request.delete_data) { $message += "`n我的数据保留在：$($request.data_dir)" }
                else { $message += "`n已核实回收的数据可在Windows回收站还原。" }
                if ($result.recovery_zip) { $message += "`n回收站恢复未全部核实，恢复ZIP保留在：$($result.recovery_zip)" }
                if ($result.errors.Count) { $message += "`n" + ($result.errors -join "`n") }
                $message += "`n结果报告：$($request.report_path)"
                [System.Windows.MessageBox]::Show($message, '合同 · 审查台移除结果') | Out-Null
            } catch { }
        }
    }
}
