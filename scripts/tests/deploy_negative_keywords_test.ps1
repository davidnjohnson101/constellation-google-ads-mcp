$ErrorActionPreference = 'Stop'
$global:NegativeDeployFixture = @{ Calls = [System.Collections.Generic.List[object]]::new(); ServiceReads = 0 }

function global:git {
    if ($args[0] -eq 'rev-parse') { '0123456789ab' }
    $global:LASTEXITCODE = 0
}

function global:Test-NegativesCloud {
    $global:NegativeDeployFixture.Calls.Add(@($args))
    $global:LASTEXITCODE = 0
    $prefix = ($args | Select-Object -First 3) -join ' '
    switch ($prefix) {
        'run services describe' {
            $global:NegativeDeployFixture.ServiceReads++
            '{"status":{"traffic":[{"revisionName":"google-ads-mcp-current","percent":100},{"revisionName":"google-ads-mcp-staged","percent":0,"tag":"negatives-review","url":"https://review.example.test"}]}}'
        }
        'run revisions describe' {
            '{"status":{"imageDigest":"us-central1-docker.pkg.dev/test/repo/image@sha256:1111111111111111111111111111111111111111111111111111111111111111"},"spec":{"serviceAccountName":"runtime@example.test","containers":[{"env":[{"name":"GOOGLE_ADS_MCP_OAUTH_CLIENT_ID","value":"test-client"},{"name":"GOOGLE_ADS_LOGIN_CUSTOMER_ID","value":"4599605095"}]}]}}'
        }
        'secrets list --filter=name:google-ads-negative-keyword-preview-key' { 'existing-secret' }
        'secrets versions list' { '1' }
        default {
            if ($args.Count -lt 3) { throw 'Cloud command arguments were lost.' }
        }
    }
}

try {
    & (Join-Path (Split-Path -Parent $PSScriptRoot) 'deploy_negative_keywords.ps1') -CloudCli Test-NegativesCloud | Out-Null
    $deploys = @($global:NegativeDeployFixture.Calls | Where-Object { $_[0] -eq 'run' -and $_[1] -eq 'deploy' })
    if ($deploys.Count -ne 1 -or $deploys[0] -notcontains '--no-traffic') { throw 'Expected exactly one staged deployment with no production traffic.' }
    if ($deploys[0] -contains '--set-env-vars' -or $deploys[0] -contains '--set-secrets') { throw 'Existing settings must be preserved.' }
    $builds = @($global:NegativeDeployFixture.Calls | Where-Object { $_[0] -eq 'builds' -and $_[1] -eq 'submit' })
    if ($builds.Count -ne 1 -or -not (($builds[0] -join ' ') -match '_BASE_IMAGE=.+@sha256:1{64}')) { throw 'The build must extend the exact serving image.' }
    if ($global:NegativeDeployFixture.ServiceReads -ne 2) { throw 'Expected post-deployment readback.' }
    'Deployment behavior test passed.'
}
finally {
    Remove-Item Function:\git -ErrorAction SilentlyContinue
    Remove-Item Function:\Test-NegativesCloud -ErrorAction SilentlyContinue
    Remove-Variable NegativeDeployFixture -Scope Global -ErrorAction SilentlyContinue
}
