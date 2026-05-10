# Infrastructure

This folder contains the AWS SAM (Serverless Application Model) templates used to deploy the EKS AI Ops Toolkit.

## Architecture Diagram

```mermaid
graph TD
    A[CloudWatch Alarms / EventBridge] -->|Trigger| B(Proactive Lambda)
    C[Slack / App Mention] -->|Webhook/Event| D(Interactive Lambda)
    
    B --> E{EKS AI Ops Toolkit Core}
    D --> E
    
    E <--> F[(DynamoDB Tables)]
    E <--> G[Amazon Bedrock / Nova]
    E <--> H[Amazon EKS Cluster]
    E --> I[GitHub Auto-fix PRs]
    E --> J[Slack Notifications]
```

To deploy the infrastructure, use `make deploy` from the root directory.
