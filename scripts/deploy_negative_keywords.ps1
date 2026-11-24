param(
    [string]$Project = 'durable-stack-502219-q0',
    [string]$Region = 'us-central1',
    [string]$Service = 'google-ads-mcp',
    [string]$PreviewSecret = 'google-ads-negative-keyword-preview-key',
    [string]$CloudCli = 'gcloud',
    [switch]$EnableManagement
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot
. (Join-Path $PSScriptRoot 'release_settings.ps1')

function Invoke-Cloud {
    param([string[]]$CloudArgs)
    $result = & $CloudCli @CloudArgs --project $Project --quiet
    if ($LASTEXITCODE -ne 0) { throw "Google Cloud command failed: $($CloudArgs[0..1] -join ' ')" }
    return $result
}

$dirty = git status --porcelain
if ($LASTEXITCODE -ne 0 -or $dirty) { throw 'Deploy a clean, reviewed Git commit.' }
$commit = (git rev-parse --short=12 HEAD).Trim()
$serviceInfo = (Invoke-Cloud @('run', 'services', 'describe', $Service, '--region', $Region, '--format=json')) | ConvertFrom-Json
$traffic = @($serviceInfo.status.traffic | Where-Object { $_.percent -gt 0 })
if ($traffic.Count -ne 1 -or $traffic[0].percent -ne 100) { throw 'Resolve split production traffic before selecting a base image.' }
$servingRevision = $traffic[0].revisionName
$revision = (Invoke-Cloud @('run', 'revisions', 'describe', $servingRevision, '--region', $Region, '--format=json')) | ConvertFrom-Json
Assert-ReleaseSettings $revision.spec $serviceInfo.spec.template.spec
$baseImage = $revision.status.imageDigest
if ($baseImage -notmatch '@sha256:[a-f0-9]{64}$') { throw 'Could not pin the exact currently serving image digest.' }
$runtimeAccount = $revision.spec.serviceAccountName
if (-not $runtimeAccount) { throw 'Could not identify the current runtime service account.' }
$existingEnv = @($revision.spec.containers[0].env)
if (-not ($existingEnv | Where-Object { $_.name -eq 'GOOGLE_ADS_MCP_OAUTH_CLIENT_ID' })) { throw 'Expected the interactive Google OAuth service.' }
if ($existingEnv | Where-Object { $_.name -eq 'GOOGLE_ADS_MCP_AUTH_MODE' -and $_.value -eq 'service_jwt' }) { throw 'Never deploy these tools to the analysis worker service.' }
if (-not ($existingEnv | Where-Object { $_.name -eq 'GOOGLE_ADS_LOGIN_CUSTOMER_ID' })) { throw 'The service needs a configured manager account.' }
if ($EnableManagement -and -not ($existingEnv | Where-Object { $_.name -eq 'GOOGLE_ADS_MCP_STORAGE_REDIS_URL' })) { throw 'Management needs durable Redis approval consumption.' }

$secretName = Invoke-Cloud @('secrets', 'list', "--filter=name:$PreviewSecret", '--format=value(name)')
if (-not $secretName) {
    Invoke-Cloud @('secrets', 'create', $PreviewSecret, '--replication-policy=automatic') | Out-Null
}
$versions = Invoke-Cloud @('secrets', 'versions', 'list', $PreviewSecret, '--filter=state:ENABLED', '--format=value(name)')
if (-not $versions) {
    $randomBytes = New-Object byte[] 48
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $generator.GetBytes($randomBytes)
    $generator.Dispose()
    $secretFile = [System.IO.Path]::GetTempFileName()
    try {
        [System.IO.File]::WriteAllText($secretFile, [Convert]::ToBase64String($randomBytes), [System.Text.UTF8Encoding]::new($false))
        Invoke-Cloud @('secrets', 'versions', 'add', $PreviewSecret, "--data-file=$secretFile") | Out-Null
    }
    finally {
        Remove-Item -LiteralPath $secretFile -Force
    }
}
Invoke-Cloud @('secrets', 'add-iam-policy-binding', $PreviewSecret, "--member=serviceAccount:$runtimeAccount", '--role=roles/secretmanager.secretAccessor') | Out-Null

$image = "$Region-docker.pkg.dev/$Project/cloud-run-source-deploy/google-ads-mcp-negatives:$commit"
Invoke-Cloud @('builds', 'submit', '.', '--config=cloudbuild.negatives.yaml', "--substitutions=_BASE_IMAGE=$baseImage,_IMAGE=$image") | Out-Null
$suffix = 'neg-' + [DateTime]::UtcNow.ToString('yyMMddHHmmss') + '-' + $commit.Substring(0, 6)
$newRevision = "$Service-$suffix"
$envUpdate = 'GOOGLE_ADS_MCP_ENABLE_NEGATIVE_KEYWORD_WRITES=true,GOOGLE_ADS_MCP_NEGATIVES_ALLOW_MCC_DESCENDANTS=true,GOOGLE_ADS_MCP_TOOLS_CONFIG=ads_mcp/tools_config.yaml'
if ($EnableManagement) { $envUpdate += ',GOOGLE_ADS_MCP_ENABLE_MANAGEMENT_WRITES=true' }
Invoke-Cloud @('run', 'deploy', $Service, '--region', $Region, '--image', $image, '--revision-suffix', $suffix, '--no-traffic', '--tag=negatives-review', '--update-env-vars', $envUpdate, '--remove-env-vars=GOOGLE_ADS_MCP_NEGATIVES_ALLOWED_CUSTOMER_IDS', '--update-secrets', "GOOGLE_ADS_MCP_NEGATIVES_SIGNING_KEY=${PreviewSecret}:latest") | Out-Null

$staged = (Invoke-Cloud @('run', 'services', 'describe', $Service, '--region', $Region, '--format=json')) | ConvertFrom-Json
$stagedRevision = (Invoke-Cloud @('run', 'revisions', 'describe', $newRevision, '--region', $Region, '--format=json')) | ConvertFrom-Json
$intendedEnvChanges = @('GOOGLE_ADS_MCP_ENABLE_NEGATIVE_KEYWORD_WRITES', 'GOOGLE_ADS_MCP_NEGATIVES_ALLOW_MCC_DESCENDANTS', 'GOOGLE_ADS_MCP_TOOLS_CONFIG', 'GOOGLE_ADS_MCP_NEGATIVES_ALLOWED_CUSTOMER_IDS', 'GOOGLE_ADS_MCP_NEGATIVES_SIGNING_KEY')
if ($EnableManagement) { $intendedEnvChanges += 'GOOGLE_ADS_MCP_ENABLE_MANAGEMENT_WRITES' }
Assert-ReleaseSettings $revision.spec $stagedRevision.spec $intendedEnvChanges
$stagedEnv = @($stagedRevision.spec.containers[0].env)
if ($EnableManagement -and -not ($stagedEnv | Where-Object { $_.name -eq 'GOOGLE_ADS_MCP_ENABLE_MANAGEMENT_WRITES' -and $_.value -eq 'true' })) { throw 'Staged management enablement was not verified.' }
if (-not ($stagedEnv | Where-Object { $_.name -eq 'GOOGLE_ADS_MCP_NEGATIVES_SIGNING_KEY' -and $_.valueFrom.secretKeyRef.name -eq $PreviewSecret })) { throw 'Staged preview secret reference was not verified.' }
$reviewUrl = ($staged.status.traffic | Where-Object { $_.tag -eq 'negatives-review' }).url
Write-Output "Staged revision: $newRevision"
Write-Output "Review URL: $reviewUrl"
Write-Output "Production traffic remains on: $servingRevision"
Write-Output 'No Google Ads keywords were changed.'
Write-Output 'After OAuth, tool discovery and validate-only checks, promote with:'
Write-Output "gcloud run services update-traffic $Service --region=$Region --project=$Project --to-revisions=${newRevision}=100"
