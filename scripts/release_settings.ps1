# Compare the live template with the traffic-serving revision without exposing
# OAuth values, Redis URLs, or secret references in command output.
function Get-ReleaseSettings {
    param($Spec, [string[]]$IgnoredEnv = @())
    $container = $Spec.containers[0]
    $envSettings = @($container.env | Where-Object { $_.name -notin $IgnoredEnv } | Sort-Object name)
    [ordered]@{
        serviceAccountName = $Spec.serviceAccountName
        containerConcurrency = $Spec.containerConcurrency
        timeoutSeconds = $Spec.timeoutSeconds
        env = $envSettings
        command = @($container.command)
        args = @($container.args)
        ports = @($container.ports)
        resources = $container.resources
        volumeMounts = @($container.volumeMounts)
        volumes = @($Spec.volumes)
    } | ConvertTo-Json -Depth 40 -Compress
}

function Assert-ReleaseSettings {
    param($Expected, $Actual, [string[]]$IgnoredEnv = @())
    $expectedSettings = Get-ReleaseSettings $Expected $IgnoredEnv
    $actualSettings = Get-ReleaseSettings $Actual $IgnoredEnv
    if ($expectedSettings -cne $actualSettings) {
        throw 'Release settings differ from the traffic-serving revision. Resolve the template/identity/configuration drift before staging or promotion.'
    }
}
