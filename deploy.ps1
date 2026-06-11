# Deploy the dev copy of wa_search.py to the live /wa skill runtime.
# The skill executes its OWN copy; edits here are invisible to /wa until deployed.
$src = Join-Path $PSScriptRoot 'wa_search.py'
$dst = Join-Path $env:USERPROFILE '.claude\skills\wa-query\wa_search.py'
Copy-Item $src $dst -Force
Write-Output "Deployed wa_search.py -> $dst"
