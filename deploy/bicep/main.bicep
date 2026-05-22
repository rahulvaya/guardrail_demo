// =============================================================================
// BankBuddy AKS infra - resource-group scoped.
//
// Provisions (all in one resource group):
//   - Log Analytics workspace (container insights)
//   - Azure Container Registry (Basic) - holds agent + guardrails images
//   - User-Assigned Managed Identity (workload identity for both pods)
//   - Azure AI Services multi-service account (kind=AIServices)
//       * exposes Content Safety + Language (`*.cognitiveservices.azure.com`)
//       * exposes the unified Foundry v2 host (`*.services.ai.azure.com`)
//       * hosts the Azure OpenAI deployment
//   - Azure AI Foundry project under the AIServices account
//   - Azure OpenAI gpt-4.1-mini model deployment on the AI Services account
//   - AKS cluster:
//       * system-assigned identity (control plane / ACR pull)
//       * OIDC issuer + Workload Identity addon enabled
//       * Container Insights -> Log Analytics
//   - Role assignments:
//       * AcrPull                          -> AKS kubelet -> ACR
//       * Cognitive Services User          -> UAMI -> AI Services (CS + Language)
//       * Cognitive Services OpenAI User   -> UAMI -> AI Services (Azure OpenAI)
//       * Azure AI User                    -> UAMI -> Foundry project (inference)
//   - Federated identity credentials on the UAMI for two KSAs:
//       system:serviceaccount:bankbuddy:guardrails
//       system:serviceaccount:bankbuddy:agent
// =============================================================================

targetScope = 'resourceGroup'

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Short suffix appended to globally-unique names.')
param nameSuffix string = uniqueString(resourceGroup().id)

@description('AKS cluster name.')
param aksName string = 'aks-bankbuddy'

@description('ACR name (globally unique, alphanumeric, 5-50 chars).')
param acrName string = toLower('acrbankbuddy${nameSuffix}')

@description('Log Analytics workspace name.')
param logAnalyticsName string = 'log-bankbuddy'

@description('Azure AI Services (multi-service) account name. Hosts Language and Azure OpenAI / Foundry.')
param aiServicesName string = toLower('aisvc-bankbuddy-${nameSuffix}')

@description('Standalone Azure AI Content Safety account name (segregated from the LLM account).')
param contentSafetyName string = toLower('cs-bankbuddy-${nameSuffix}')

@description('Azure AI Foundry project name (lives under the AIServices account).')
param foundryProjectName string = 'bankbuddy-foundry'

@description('Azure OpenAI model deployment name used by the agent.')
param openAiDeploymentName string = 'gpt-4.1-mini'

@description('Azure OpenAI model name to deploy.')
param openAiModelName string = 'gpt-4.1-mini'

@description('Azure OpenAI model version.')
param openAiModelVersion string = '2025-04-14'

@description('Azure OpenAI deployment capacity (k TPM).')
@minValue(1)
param openAiCapacity int = 30

@description('User-assigned managed identity used by both workload pods.')
param workloadIdentityName string = 'id-bankbuddy-workload'

@description('Kubernetes namespace that the workloads run in.')
param k8sNamespace string = 'bankbuddy'

@description('Node VM size for the AKS system pool.')
param nodeVmSize string = 'Standard_D2s_v3'

@description('Node count for the AKS system pool.')
@minValue(1)
@maxValue(5)
param nodeCount int = 1

@description('Kubernetes version. Leave empty to use AKS default.')
param kubernetesVersion string = ''

// ---------------------------------------------------------------------------
// Built-in role definition IDs
// ---------------------------------------------------------------------------
var acrPullRoleId                  = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var cognitiveServicesUserRoleId    = 'a97b65f3-24c7-4388-baec-2e87135dc908'
var cognitiveServicesOpenAiUserId  = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
var azureAiUserRoleId              = '53ca6127-db72-4b80-b1b0-d745d6d5456d'

// ---------------------------------------------------------------------------
// Log Analytics
// ---------------------------------------------------------------------------
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

// ---------------------------------------------------------------------------
// ACR
// ---------------------------------------------------------------------------
resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: acrName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

// ---------------------------------------------------------------------------
// User-Assigned Managed Identity (workload identity for both pods)
// ---------------------------------------------------------------------------
resource workloadIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: workloadIdentityName
  location: location
}

// ---------------------------------------------------------------------------
// Azure AI Services (multi-service) - Content Safety + Language + Azure OpenAI.
// Local auth (keys) is DISABLED so only AAD / managed identity works.
// ---------------------------------------------------------------------------
resource aiServices 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' = {
  name: aiServicesName
  location: location
  sku: {
    name: 'S0'
  }
  kind: 'AIServices'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: aiServicesName
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true
    allowProjectManagement: true
  }
}

resource openAiDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: aiServices
  name: openAiDeploymentName
  sku: {
    name: 'GlobalStandard'
    capacity: openAiCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: openAiModelName
      version: openAiModelVersion
    }
  }
}

// ---------------------------------------------------------------------------
// Standalone Azure AI Content Safety account (segregated from the LLM account).
// Hosts Hate / Self-harm / Sexual / Violence + Prompt Shield. Local auth
// disabled so only AAD / managed identity works.
// ---------------------------------------------------------------------------
resource contentSafety 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: contentSafetyName
  location: location
  sku: {
    name: 'S0'
  }
  kind: 'ContentSafety'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: contentSafetyName
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true
  }
}

// ---------------------------------------------------------------------------
// Azure AI Foundry project (v2). Lives under the AIServices account; gives
// callers a project-scoped data-plane endpoint at
//   https://<account>.services.ai.azure.com/api/projects/<project>
// usable with the `azure-ai-projects` SDK and as a LiteLLM `api_base`.
// ---------------------------------------------------------------------------
resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' = {
  parent: aiServices
  name: foundryProjectName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {}
}

// ---------------------------------------------------------------------------
// AKS - OIDC issuer + Workload Identity addon enabled
// ---------------------------------------------------------------------------
resource aks 'Microsoft.ContainerService/managedClusters@2024-05-01' = {
  name: aksName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    dnsPrefix: 'aks-bankbuddy-${nameSuffix}'
    kubernetesVersion: empty(kubernetesVersion) ? null : kubernetesVersion
    enableRBAC: true
    oidcIssuerProfile: {
      enabled: true
    }
    securityProfile: {
      workloadIdentity: {
        enabled: true
      }
    }
    agentPoolProfiles: [
      {
        name: 'system'
        mode: 'System'
        count: nodeCount
        vmSize: nodeVmSize
        osType: 'Linux'
        osSKU: 'AzureLinux'
        type: 'VirtualMachineScaleSets'
        enableAutoScaling: false
      }
    ]
    networkProfile: {
      networkPlugin: 'azure'
      networkPluginMode: 'overlay'
      loadBalancerSku: 'standard'
    }
    addonProfiles: {
      omsagent: {
        enabled: true
        config: {
          logAnalyticsWorkspaceResourceID: logAnalytics.id
        }
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Federated identity credentials: bind the UAMI to two KSAs.
// ---------------------------------------------------------------------------
resource fedCredGuardrails 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: workloadIdentity
  name: 'guardrails'
  properties: {
    issuer: aks.properties.oidcIssuerProfile.issuerURL
    subject: 'system:serviceaccount:${k8sNamespace}:guardrails'
    audiences: [
      'api://AzureADTokenExchange'
    ]
  }
}

resource fedCredAgent 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: workloadIdentity
  name: 'agent'
  // Federated credential writes on the same UAMI must be serialized -
  // Azure rejects concurrent writes with ConcurrentFederatedIdentityCredentialsWritesForSingleManagedIdentity.
  dependsOn: [
    fedCredGuardrails
  ]
  properties: {
    issuer: aks.properties.oidcIssuerProfile.issuerURL
    subject: 'system:serviceaccount:${k8sNamespace}:agent'
    audiences: [
      'api://AzureADTokenExchange'
    ]
  }
}

// ---------------------------------------------------------------------------
// Role assignments
// ---------------------------------------------------------------------------
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, aks.id, 'acrpull')
  scope: acr
  properties: {
    principalId: aks.properties.identityProfile.kubeletidentity.objectId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

resource cogSvcUserAssign 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(aiServices.id, workloadIdentity.id, 'cogsvc-user')
  scope: aiServices
  properties: {
    principalId: workloadIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUserRoleId)
  }
}

resource contentSafetyUserAssign 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(contentSafety.id, workloadIdentity.id, 'cogsvc-user')
  scope: contentSafety
  properties: {
    principalId: workloadIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUserRoleId)
  }
}

resource openAiUserAssign 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(aiServices.id, workloadIdentity.id, 'openai-user')
  scope: aiServices
  properties: {
    principalId: workloadIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesOpenAiUserId)
  }
}

resource azureAiUserAssign 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryProject.id, workloadIdentity.id, 'azure-ai-user')
  scope: foundryProject
  properties: {
    principalId: workloadIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', azureAiUserRoleId)
  }
}

// ---------------------------------------------------------------------------
// Outputs - consumed by the deploy scripts.
// ---------------------------------------------------------------------------
output aksName              string = aks.name
output acrName              string = acr.name
output acrLoginServer       string = acr.properties.loginServer
output resourceGroupName    string = resourceGroup().name

output workloadIdentityName     string = workloadIdentity.name
output workloadIdentityClientId string = workloadIdentity.properties.clientId
output aksOidcIssuerUrl         string = aks.properties.oidcIssuerProfile.issuerURL

output aiServicesName            string = aiServices.name
output aiServicesEndpoint        string = aiServices.properties.endpoint
output aiServicesOpenAiEndpoint  string = 'https://${aiServices.name}.openai.azure.com/'
output aiServicesUnifiedEndpoint string = 'https://${aiServices.name}.services.ai.azure.com/'
output contentSafetyName         string = contentSafety.name
output contentSafetyEndpoint     string = contentSafety.properties.endpoint
output foundryProjectName        string = foundryProject.name
output foundryProjectEndpoint    string = 'https://${aiServices.name}.services.ai.azure.com/api/projects/${foundryProject.name}'
output openAiDeploymentName      string = openAiDeployment.name
