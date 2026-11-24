"""Exercise the actual PowerShell release-setting guard with synthetic specs."""

import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("pwsh"), "PowerShell deployment helper")
class DeploymentSettingsTest(unittest.TestCase):
    def test_rejects_oauth_redis_identity_or_scope_drift(self):
        helper = Path("scripts/release_settings.ps1").resolve()
        script = """
param($Helper)
$ErrorActionPreference = 'Stop'
. $Helper
$spec = '{"serviceAccountName":"runtime@example.test","containers":[{"image":"old","env":[{"name":"OAUTH","value":"same"},{"name":"REDIS","value":"same"},{"name":"SCOPE","value":"same"}]}]}' | ConvertFrom-Json
Assert-ReleaseSettings $spec $spec
foreach ($field in @('OAUTH','REDIS','SCOPE','identity')) {
    $other = $spec | ConvertTo-Json -Depth 30 | ConvertFrom-Json
    if ($field -eq 'identity') { $other.serviceAccountName = 'other@example.test' }
    else { ($other.containers[0].env | Where-Object name -eq $field).value = 'changed' }
    $caught = $false
    try { Assert-ReleaseSettings $spec $other } catch { $caught = $true }
    if (-not $caught) { throw "Missed drift: $field" }
}
$other = $spec | ConvertTo-Json -Depth 30 | ConvertFrom-Json
$other.containers[0].image = 'new-image'
$other.containers[0].env = @($other.containers[0].env | Sort-Object name -Descending)
Assert-ReleaseSettings $spec $other
Write-Output 'Release setting guard passed'
"""
        # Write a test-only runner; argument paths are passed separately.
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            runner = Path(directory) / "test.ps1"
            runner.write_text(script, encoding="utf-8")
            result = subprocess.run(
                [
                    shutil.which("pwsh"),
                    "-NoProfile",
                    "-File",
                    str(runner),
                    str(helper),
                ],
                text=True,
                capture_output=True,
                timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Release setting guard passed", result.stdout)
