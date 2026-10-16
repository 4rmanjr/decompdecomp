# decompdecomp's dev branch, for testing what isn't released yet (Windows, PowerShell):
#
#   irm https://raw.githubusercontent.com/n-delic/decomp.wtf/main/site/public/dev.ps1 | iex
#
# Runs the dev branch's own installer (install.ps1 on GitHub) with DECOMP_BRANCH=dev: decompdecomp installs from the
# branch's newest commit, and the app then offers every new commit pushed there as an update (one click).
# Back to releases: irm https://raw.githubusercontent.com/n-delic/decomp.wtf/main/site/public/install.ps1 | iex
& {
$branch = if ($env:DECOMP_BRANCH) { $env:DECOMP_BRANCH } else { "dev" }
$had = $env:DECOMP_BRANCH
$env:DECOMP_BRANCH = $branch
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $script = Invoke-RestMethod -UseBasicParsing -UserAgent "decompdecomp-installer (PowerShell)" "https://raw.githubusercontent.com/n-delic/decomp.wtf/$branch/site/public/install.ps1"
    Invoke-Expression $script
} finally {
    $env:DECOMP_BRANCH = $had
}
}
