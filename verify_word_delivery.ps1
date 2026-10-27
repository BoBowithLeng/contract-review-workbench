param(
    [Parameter(Mandatory=$true)][string]$BundleDirectory
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $BundleDirectory).Path
$required = @('00-原稿.docx','01-洁净版.docx','02-修订版.docx','03-原稿批注版.docx','04-修订与批注版.docx','05-交付核对报告.docx','manifest.json')
foreach ($name in $required) {
    if (-not (Test-Path -LiteralPath (Join-Path $root $name))) { throw "缺少交付文件：$name" }
}
$output = Join-Path $root 'word-validation'
if (Test-Path -LiteralPath $output) { throw 'word-validation 已存在，请保留先前验证，不覆盖。' }
[void](New-Item -ItemType Directory -Path $output)
$word = $null
$ownsApplication = $false
$opened = [System.Collections.Generic.List[object]]::new()
$results = [ordered]@{ client='Microsoft Word'; opened=@(); checks=[ordered]@{} }
function Open-TestDocument([string]$name) {
    # New hidden automation instance; only the six explicitly named test files.
    $doc = $word.Documents.Open((Join-Path $root $name), $false, $true, $false)
    $opened.Add($doc)
    return $doc
}
function Get-ContractText($doc) { return [string]$doc.Content.Text }
function Check([bool]$pass, [string]$name) {
    $results.checks[$name] = $pass
    if (-not $pass) { throw "Word 验证失败：$name" }
}
try {
    $word = New-Object -ComObject Word.Application
    if ($word.Documents.Count -ne 0 -or $word.ProtectedViewWindows.Count -ne 0) {
        throw '自动化连接到了已有文档的Word实例；不改动或关闭该实例。'
    }
    $ownsApplication = $true
    $word.Visible = $false
    $word.DisplayAlerts = 0
    $word.AutomationSecurity = 3
    $results.version = [string]$word.Version
    $manifest = Get-Content -LiteralPath (Join-Path $root 'manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $results.source_sha256 = $manifest.source_sha256
    $results.plan_sha256 = $manifest.plan_sha256
    $original = Open-TestDocument '00-原稿.docx'
    $originalText = Get-ContractText $original
    $clean = Open-TestDocument '01-洁净版.docx'
    $cleanText = Get-ContractText $clean
    Check ($clean.Revisions.Count -eq 0 -and $clean.Comments.Count -eq 0) 'clean_has_no_revisions_or_comments'
    $tracked = Open-TestDocument '02-修订版.docx'
    $results.original_revision_count = $tracked.Revisions.Count
    Check ($tracked.Revisions.Count -ge $manifest.edit_ids.Count) 'native_revisions_visible_to_word'
    $tracked.Revisions.AcceptAll()
    Check ((Get-ContractText $tracked) -eq $cleanText) 'accept_all_equals_clean'
    $acceptedPath = Join-Path $output 'accepted.docx'
    $tracked.SaveAs2($acceptedPath, 16)
    $tracked.Close(0)
    [void]$opened.Remove($tracked)
    $accepted = $word.Documents.Open($acceptedPath,$false,$true,$false)
    $opened.Add($accepted)
    Check ((Get-ContractText $accepted) -eq $cleanText -and $accepted.Revisions.Count -eq 0) 'accepted_saved_and_reopened'
    $reject = Open-TestDocument '02-修订版.docx'
    $reject.Revisions.RejectAll()
    Check ((Get-ContractText $reject) -eq $originalText) 'reject_all_equals_original'
    $reject.SaveAs2((Join-Path $output 'rejected.docx'),16)
    $comments = Open-TestDocument '03-原稿批注版.docx'
    Check ((Get-ContractText $comments) -eq $originalText) 'comments_variant_preserves_original_text'
    Check ($comments.Comments.Count -eq $manifest.comment_ids.Count) 'native_comments_visible_to_word'
    $results.comments = @()
    foreach ($comment in $comments.Comments) {
        Check ($comment.Scope.Text.Length -gt 0) ('comment_anchor_' + $comment.Index)
        $results.comments += @{ text=[string]$comment.Range.Text; scope=[string]$comment.Scope.Text }
    }
    $combined = Open-TestDocument '04-修订与批注版.docx'
    Check ($combined.Comments.Count -eq $manifest.comment_ids.Count -and $combined.Revisions.Count -gt 0) 'combined_contains_both'
    $combined.Revisions.AcceptAll()
    Check ((Get-ContractText $combined) -eq $cleanText) 'combined_accept_equals_clean'
    Check ($combined.Comments.Count -eq $manifest.comment_ids.Count) 'comments_survive_acceptance'
    $combinedPath = Join-Path $output 'accepted-with-comments.docx'
    $combined.SaveAs2($combinedPath,16)
    $combined.Close(0)
    [void]$opened.Remove($combined)
    $reopened = $word.Documents.Open($combinedPath,$false,$true,$false)
    $opened.Add($reopened)
    Check ($reopened.Comments.Count -eq $manifest.comment_ids.Count) 'comments_survive_save_reopen'
    foreach ($comment in $reopened.Comments) {
        Check ($comment.Scope.Text.Length -gt 0) ('reopened_comment_anchor_' + $comment.Index)
    }
    $report = Open-TestDocument '05-交付核对报告.docx'
    Check ((Get-ContractText $report).Contains($manifest.plan_sha256)) 'report_uses_same_plan'
    $results.opened = $required | Where-Object { $_ -like '*.docx' }
    $results.status = 'passed'
} catch {
    $results.status = 'failed'
    $results.error = $_.Exception.Message
    throw
} finally {
    foreach ($document in $opened) {
        try { $document.Close(0) } catch {}
        try { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($document) } catch {}
    }
    if ($null -ne $word) {
        if ($ownsApplication) { try { $word.Quit(0) } catch {} }
        try { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($word) } catch {}
    }
    $results | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $output 'result.json') -Encoding UTF8
}
$results | ConvertTo-Json -Depth 6
