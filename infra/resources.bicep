param environmentName string
param location string
param deploymentProfile string
param fabricSqlServer string
param fabricSqlDatabase string
param allowedIpRanges string
param principalId string
param dabImage string
param allowedClientAppIds string

var isSecure = deploymentProfile == 'secure'
var token = uniqueString(subscription().id, resourceGroup().id, environmentName)
var tags = { 'azd-env-name': environmentName, 'mcp-profile': deploymentProfile }

// Produced by scripts/build_profiles.py (the azd preprovision hook) from dab/dab-config.json.
var dabConfig = isSecure
  ? loadTextContent('generated/dab-config.secure.json')
  : loadTextContent('generated/dab-config.poc.json')

var ipRanges = filter(map(split(allowedIpRanges, ','), r => trim(r)), r => !empty(r))
var ipRules = [
  for (r, i) in ipRanges: {
    name: 'allow-${i}'
    action: 'Allow'
    ipAddressRange: contains(r, '/') ? r : '${r}/32'
  }
]

var issuer = '${environment().authentication.loginEndpoint}${tenant().tenantId}/v2.0'

// Azure CLI's well-known client ID, so `az login` users can mint tokens for manual tests.
var azureCliClientId = '04b07795-8ddb-461a-bbee-02f9e1bf7b46'
var extraClientAppIds = filter(map(split(allowedClientAppIds, ','), c => trim(c)), c => !empty(c))

// ---------------------------------------------------------------- shared

module logs 'br/public:avm/res/operational-insights/workspace:0.16.1' = {
  name: 'logs'
  params: {
    name: 'log-${token}'
    location: location
    tags: tags
  }
}

// The identity DAB uses to connect to Fabric. Granted a Fabric workspace role by the postprovision hook.
module dabIdentity 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = {
  name: 'dab-identity'
  params: {
    name: 'id-dab-${token}'
    location: location
    tags: tags
  }
}

// ---------------------------------------------------------------- secure-only networking

module vnet 'br/public:avm/res/network/virtual-network:0.10.2' = if (isSecure) {
  name: 'vnet'
  params: {
    name: 'vnet-${token}'
    location: location
    tags: tags
    addressPrefixes: ['10.40.0.0/16']
    subnets: [
      {
        name: 'aca-infra'
        addressPrefix: '10.40.0.0/23'
        delegation: 'Microsoft.App/environments'
      }
    ]
  }
}

// ---------------------------------------------------------------- container apps environment

module env 'br/public:avm/res/app/managed-environment:0.16.0' = {
  name: 'aca-env'
  params: {
    name: 'cae-${token}'
    location: location
    tags: tags
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsWorkspaceResourceId: logs.outputs.resourceId
    }
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
    // AVM defaults are zone-redundant (needs a VNet) and public access Disabled. Set both explicitly.
    zoneRedundant: false
    internal: isSecure
    infrastructureSubnetResourceId: vnet.?outputs.subnetResourceIds[0]
    publicNetworkAccess: isSecure ? 'Disabled' : 'Enabled'
  }
}

// Lets anything else in (or peered to) the VNet resolve the internal environment's hostnames.
module dns 'br/public:avm/res/network/private-dns-zone:0.8.1' = if (isSecure) {
  name: 'aca-dns'
  params: {
    name: env.outputs.defaultDomain
    tags: tags
    a: [
      {
        name: '*'
        ttl: 300
        aRecords: [{ ipv4Address: env.outputs.staticIp }]
      }
    ]
    virtualNetworkLinks: [
      {
        virtualNetworkResourceId: vnet!.outputs.resourceId
        registrationEnabled: false
      }
    ]
  }
}

// ---------------------------------------------------------------- secure-only identity

// Separate identity for the test job: it calls the endpoint but never touches Fabric.
module testIdentity 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = if (isSecure) {
  name: 'test-identity'
  params: {
    name: 'id-mcptest-${token}'
    location: location
    tags: tags
  }
}

module entra 'modules/entra.bicep' = if (isSecure) {
  name: 'entra'
  params: {
    uniqueName: 'fabric-mcp-${token}'
    displayName: 'Fabric MCP (${environmentName})'
    readerPrincipalIds: concat([testIdentity!.outputs.principalId], empty(principalId) ? [] : [principalId])
  }
}

// ---------------------------------------------------------------- DAB

var connectionString = 'Server=tcp:${fabricSqlServer},1433;Database=${fabricSqlDatabase};Authentication=Active Directory Managed Identity;User Id=${dabIdentity.outputs.clientId};Encrypt=True;TrustServerCertificate=False;Connection Timeout=30;'

var jwtEnv = isSecure
  ? [
      { name: 'DAB_JWT_AUDIENCE', value: entra!.outputs.appId }
      { name: 'DAB_JWT_ISSUER', value: issuer }
    ]
  : []

module dab 'br/public:avm/res/app/container-app:0.23.0' = {
  name: 'dab'
  params: {
    name: 'ca-dab-${token}'
    location: location
    tags: union(tags, { 'azd-service-name': 'dab' })
    environmentResourceId: env.outputs.resourceId
    workloadProfileName: 'Consumption'
    managedIdentities: { userAssignedResourceIds: [dabIdentity.outputs.resourceId] }
    secrets: [
      { name: 'fabric-conn', value: connectionString }
      { name: 'dab-config', value: dabConfig }
    ]
    volumes: [
      {
        name: 'dab-config'
        storageType: 'Secret'
        secrets: [{ secretRef: 'dab-config', path: 'dab-config.json' }]
      }
    ]
    containers: [
      {
        name: 'dab'
        image: dabImage
        args: ['--ConfigFileName', '/config/dab-config.json']
        resources: { cpu: json('0.5'), memory: '1Gi' }
        env: concat(
          [
            { name: 'FABRIC_CONN', secretRef: 'fabric-conn' }
            // Secret updates alone do not roll a revision; this does whenever the config changes.
            { name: 'DAB_CONFIG_HASH', value: uniqueString(dabConfig) }
          ],
          jwtEnv
        )
        volumeMounts: [{ volumeName: 'dab-config', mountPath: '/config' }]
      }
    ]
    ingressExternal: true
    ingressTargetPort: 5000
    ingressAllowInsecure: false
    ipSecurityRestrictions: isSecure ? [] : ipRules
    // DAB MCP sessions live in memory; one replica keeps a client on the process that knows its session.
    scaleSettings: { minReplicas: 1, maxReplicas: 1 }
    // Secure: reject tokenless requests at the edge. DAB alone still answers the MCP
    // handshake and describe_entities anonymously under the EntraID provider.
    authConfig: isSecure
      ? {
          platform: { enabled: true }
          globalValidation: { unauthenticatedClientAction: 'Return401' }
          identityProviders: {
            azureActiveDirectory: {
              enabled: true
              registration: {
                clientId: entra!.outputs.appId
                openIdIssuer: issuer
              }
              validation: {
                allowedAudiences: [entra!.outputs.appId, entra!.outputs.identifierUri]
                // Left empty, ACA accepts only tokens the API app minted for itself (azp = its own client ID).
                defaultAuthorizationPolicy: {
                  allowedApplications: concat([testIdentity!.outputs.clientId, azureCliClientId], extraClientAppIds)
                }
              }
            }
          }
        }
      : null
  }
}

// ---------------------------------------------------------------- secure-only test job

var testCommand = 'pip install --quiet --disable-pip-version-check --root-user-action=ignore mcp==1.26.0 httpx==0.28.1 azure-identity==1.25.3 && python /scripts/test_endpoint.py --url https://${dab.outputs.fqdn}/mcp --audience ${isSecure ? entra!.outputs.identifierUri : ''}'

module testJob 'br/public:avm/res/app/job:0.7.2' = if (isSecure) {
  name: 'test-job'
  params: {
    name: 'caj-mcptest-${token}'
    location: location
    tags: tags
    environmentResourceId: env.outputs.resourceId
    workloadProfileName: 'Consumption'
    triggerType: 'Manual'
    manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
    replicaTimeout: 600
    replicaRetryLimit: 0
    managedIdentities: { userAssignedResourceIds: [testIdentity!.outputs.resourceId] }
    secrets: [{ name: 'test-endpoint-py', value: loadTextContent('../scripts/test_endpoint.py') }]
    volumes: [
      {
        name: 'scripts'
        storageType: 'Secret'
        secrets: [{ secretRef: 'test-endpoint-py', path: 'test_endpoint.py' }]
      }
    ]
    containers: [
      {
        name: 'test'
        image: 'mcr.microsoft.com/devcontainers/python:3.12'
        command: ['/bin/sh', '-c', testCommand]
        resources: { cpu: '0.5', memory: '1Gi' }
        env: [{ name: 'AZURE_CLIENT_ID', value: testIdentity!.outputs.clientId }]
        volumeMounts: [{ volumeName: 'scripts', mountPath: '/scripts' }]
      }
    ]
  }
}

output mcpEndpoint string = 'https://${dab.outputs.fqdn}/mcp'
output dabIdentityPrincipalId string = dabIdentity.outputs.principalId
output dabIdentityName string = dabIdentity.outputs.name
output entraAppId string = isSecure ? entra!.outputs.appId : ''
output entraApiUri string = isSecure ? entra!.outputs.identifierUri : ''
output testJobName string = isSecure ? testJob!.outputs.name : ''
