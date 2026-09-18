// Cost guardrail. On a metered subscription this is not optional: an AKS pool
// and a VM left running over a weekend cost more than the entire planned API
// budget for the study.

targetScope = 'subscription'

param budgetName string
param amountUsd int
param contactEmail string

@description('First day of the budget period, YYYY-MM-DD. Must be the first of a month.')
param startDate string

resource budget 'Microsoft.Consumption/budgets@2023-05-01' = {
  name: budgetName
  properties: {
    category: 'Cost'
    amount: amountUsd
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: startDate
    }
    notifications: {
      Actual50: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 50
        contactEmails: [contactEmail]
        thresholdType: 'Actual'
      }
      Actual80: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 80
        contactEmails: [contactEmail]
        thresholdType: 'Actual'
      }
      Forecast100: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 100
        contactEmails: [contactEmail]
        thresholdType: 'Forecasted'
      }
    }
  }
}
