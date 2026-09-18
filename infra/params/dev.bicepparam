using '../main.bicep'

// Copy to dev.local.bicepparam and fill in the blanks. The committed file
// carries no secrets: the password and SSH key are read from the environment at
// deploy time so they never enter git history.

param location = 'polandcentral'
param resourceGroupName = 'rg-agentfactory-dev'
param namePrefix = 'agentfac'
param environment = 'dev'

// az deployment sub create ... reads these from the shell:
//   export AF_PG_PASSWORD='...'            (or $env:AF_PG_PASSWORD in PowerShell)
//   export AF_SSH_PUBLIC_KEY="$(cat ~/.ssh/id_ed25519.pub)"
param postgresAdminPassword = readEnvironmentVariable('AF_PG_PASSWORD')
param sshPublicKey = readEnvironmentVariable('AF_SSH_PUBLIC_KEY')

// az ad signed-in-user show --query id -o tsv
param adminPrincipalId = readEnvironmentVariable('AF_ADMIN_PRINCIPAL_ID', '')

param monthlyBudgetUsd = 150
param budgetContactEmail = 'bobur_yusupov@itpu.uz'
param budgetStartDate = '2026-10-01'

param deployAks = true
