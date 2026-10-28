# Run inside the extracted project folder on Windows after installing Git and GitHub CLI.
$ErrorActionPreference = 'Stop'
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Install Git for Windows first.' }
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw 'Install GitHub CLI first: https://cli.github.com/' }

gh auth status
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Sign in to GitHub:'
    gh auth login
    if ($LASTEXITCODE -ne 0) { throw 'GitHub authentication failed.' }
}

if (-not (Test-Path .git)) { git init -b main }
git add .
if ($LASTEXITCODE -ne 0) { throw 'git add failed.' }
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git commit -m 'feat: announce openOMSI releases on Discord'
    if ($LASTEXITCODE -ne 0) { throw 'git commit failed. Configure git user.name and user.email if needed.' }
}

gh repo create isaacsa2/openomsi-discord-releases --public --description 'Automatic Discord announcements for official openOMSI GitHub releases' --source=. --remote=origin --push
if ($LASTEXITCODE -ne 0) { throw 'Repository creation failed. Check permissions or whether the repository already exists.' }
Write-Host 'Repository published! Next: set DISCORD_WEBHOOK_URL in GitHub Settings > Secrets and variables > Actions.'
