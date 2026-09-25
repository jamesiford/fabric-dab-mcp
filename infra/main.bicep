targetScope = 'subscription'

@minLength(1)
@maxLength(40)
@description('azd environment name. Used to name the resource group and derive resource names.')
param environmentName string

@minLength(1)
param location string

@allowed(['poc', 'secure'])
@description('poc = public ingress behind an IP allow-list, anonymous read. secure = VNet-internal ingress, Entra ID required, in-VNet test job.')
param deploymentProfile string = 'poc'

@description('Fabric SQL analytics endpoint host, e.g. xxxx.datawarehouse.fabric.microsoft.com')
param fabricSqlServer string

@description('Lakehouse or warehouse name on that endpoint.')
param fabricSqlDatabase string

@description('PoC only. Comma-separated CIDRs allowed to reach the endpoint. The preprovision hook defaults this to your public IP.')
param allowedIpRanges string = ''

@description('Object ID of the deploying user. In the secure profile they are granted the reader app role so they can call the endpoint from inside the VNet.')
param principalId string = ''

param dabImage string = 'mcr.microsoft.com/azure-databases/data-api-builder:2.0.9'

@description('Secure only. Comma-separated client (application) IDs allowed to call the endpoint, e.g. a Foundry project identity. The test job and Azure CLI are always allowed.')
param allowedClientAppIds string = ''

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: { 'azd-env-name': environmentName }
}

module resources 'resources.bicep' = {
  scope: rg
  name: 'resources'
  params: {
    environmentName: environmentName
    location: location
    deploymentProfile: deploymentProfile
    fabricSqlServer: fabricSqlServer
    fabricSqlDatabase: fabricSqlDatabase
    allowedIpRanges: allowedIpRanges
    principalId: principalId
    dabImage: dabImage
    allowedClientAppIds: allowedClientAppIds
  }
}

output AZURE_RESOURCE_GROUP string = rg.name
output DEPLOYMENT_PROFILE string = deploymentProfile
output MCP_ENDPOINT string = resources.outputs.mcpEndpoint
output DAB_IDENTITY_PRINCIPAL_ID string = resources.outputs.dabIdentityPrincipalId
output DAB_IDENTITY_NAME string = resources.outputs.dabIdentityName
output ENTRA_APP_ID string = resources.outputs.entraAppId
output ENTRA_API_URI string = resources.outputs.entraApiUri
output TEST_JOB_NAME string = resources.outputs.testJobName
