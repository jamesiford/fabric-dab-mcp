// Entra ID app registration that callers authenticate against (secure profile only).
extension 'br:mcr.microsoft.com/bicep/extensions/microsoftgraph/v1.0:1.0.0'

@description('Deploy-time-constant key that makes the app registration idempotent across runs.')
param uniqueName string

param displayName string

@description('Principal (object) IDs granted the reader app role, e.g. the test job identity and the deploying user.')
param readerPrincipalIds string[]

var readerRoleId = guid(uniqueName, 'DepositsReader')
var accessScopeId = guid(uniqueName, 'access_as_user')
var identifierUri = 'api://${tenant().tenantId}/${uniqueName}'

// Azure CLI's well-known client ID. Pre-authorising it lets `az login` users and
// DefaultAzureCredential mint tokens for this API without a consent prompt.
var azureCliClientId = '04b07795-8ddb-461a-bbee-02f9e1bf7b46'

resource app 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: uniqueName
  displayName: displayName
  signInAudience: 'AzureADMyOrg'
  identifierUris: [identifierUri]
  api: {
    requestedAccessTokenVersion: 2
    oauth2PermissionScopes: [
      {
        id: accessScopeId
        value: 'access_as_user'
        type: 'User'
        isEnabled: true
        adminConsentDisplayName: 'Query the deposits MCP endpoint'
        adminConsentDescription: 'Lets the signed-in user call read-only MCP tools over curated deposit views.'
        userConsentDisplayName: 'Query the deposits MCP endpoint'
        userConsentDescription: 'Lets you call read-only MCP tools over curated deposit views.'
      }
    ]
    preAuthorizedApplications: [
      {
        appId: azureCliClientId
        delegatedPermissionIds: [accessScopeId]
      }
    ]
  }
  appRoles: [
    {
      id: readerRoleId
      value: 'Deposits.Read'
      displayName: 'Deposits reader'
      description: 'Can call the read-only MCP tools.'
      allowedMemberTypes: ['User', 'Application']
      isEnabled: true
    }
  ]
}

resource sp 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: app.appId
  // Only principals explicitly assigned an app role can get a token at all.
  appRoleAssignmentRequired: true
}

resource readers 'Microsoft.Graph/appRoleAssignedTo@v1.0' = [
  for id in readerPrincipalIds: {
    appRoleId: readerRoleId
    principalId: id
    resourceId: sp.id
  }
]

output appId string = app.appId
output identifierUri string = identifierUri
output servicePrincipalId string = sp.id
output readerRoleId string = readerRoleId
