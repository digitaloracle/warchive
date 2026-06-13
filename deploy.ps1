# Deploy the dev copies to the live /wa skill runtime.
# The skill executes its OWN copy; edits here are invisible to /wa until deployed.
# wa_search.py imports the retrieval modules, so they must ship alongside it.
$dstDir = Join-Path $env:USERPROFILE '.claude\skills\wa-query'
foreach ($f in 'wa_search.py', 'wa_normalize.py', 'wa_translit.py', 'wa_embed.py') {
    Copy-Item (Join-Path $PSScriptRoot $f) (Join-Path $dstDir $f) -Force
    Write-Output "Deployed $f"
}
Write-Output "-> $dstDir"
