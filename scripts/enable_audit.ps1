# Enable Microsoft 365 unified audit logging (needed for Copilot Chat CopilotInteraction records).
# Signs in with a device code as the given admin, enables auditing, and reads the setting back.
param([Parameter(Mandatory)][string]$UserPrincipalName)
$ErrorActionPreference = 'Stop'
Import-Module ExchangeOnlineManagement
Connect-ExchangeOnline -Device -UserPrincipalName $UserPrincipalName -ShowBanner:$false
try {
    $before = (Get-AdminAuditLogConfig).UnifiedAuditLogIngestionEnabled
    Write-Output "UnifiedAuditLogIngestionEnabled before: $before"
    if (-not $before) {
        Write-Output 'Running Enable-OrganizationCustomization (one-time tenant hydration).'
        try {
            Enable-OrganizationCustomization -ErrorAction Stop
        } catch {
            if ($_.Exception.Message -notmatch 'already|not required|isn.t required') { throw }
            Write-Output "Organization customization: $($_.Exception.Message.Split([Environment]::NewLine)[0])"
        }
        for ($attempt = 1; $attempt -le 10; $attempt++) {
            try {
                Set-AdminAuditLogConfig -UnifiedAuditLogIngestionEnabled $true -ErrorAction Stop
                break
            } catch {
                if ($_.Exception.Message -notmatch 'Enable-OrganizationCustomization' -or $attempt -eq 10) { throw }
                Write-Output "Hydration still propagating (attempt $attempt); retrying in 60 seconds."
                Start-Sleep 60
            }
        }
    }
    $after = (Get-AdminAuditLogConfig).UnifiedAuditLogIngestionEnabled
    Write-Output "UnifiedAuditLogIngestionEnabled after: $after"
    if (-not $after) { throw 'Audit logging is still disabled after the change.' }
} finally {
    Disconnect-ExchangeOnline -Confirm:$false
}
