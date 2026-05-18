# AWS Cleanup Guide

This guide provides complete instructions for removing all AWS resources deployed by the EKS AI Ops Toolkit.

## Quick Cleanup

Run the comprehensive cleanup command:

```bash
make destroy-all
```

This will remove:
- CloudFormation stacks (eks-ai-ops-toolkit, eksctl resources, SAM resources)
- EC2 instances
- Lambda functions
- DynamoDB tables
- SSM parameters
- CloudWatch log groups
- IAM roles (non-AWS service roles)

## Detailed Cleanup Process

### 1. Delete CloudFormation Stacks

The toolkit creates multiple CloudFormation stacks depending on deployment configuration:

```bash
# Primary stack
aws cloudformation delete-stack --stack-name eks-ai-ops-toolkit --region us-east-1

# SAM-managed stack
aws cloudformation delete-stack --stack-name aws-sam-cli-managed-default --region us-east-1

# eksctl-managed EKS cluster (nodegroup first, then cluster)
aws cloudformation delete-stack --stack-name eksctl-eks-ai-ops-nodegroup-ng-632887e6 --region us-east-1
aws cloudformation delete-stack --stack-name eksctl-eks-ai-ops-cluster --region us-east-1

# Disable termination protection if needed
aws cloudformation update-termination-protection --no-enable-termination-protection \
  --stack-name eksctl-eks-ai-ops-nodegroup-ng-632887e6 --region us-east-1
aws cloudformation update-termination-protection --no-enable-termination-protection \
  --stack-name eksctl-eks-ai-ops-cluster --region us-east-1
```

### 2. Terminate EC2 Instances

Find and terminate any remaining EC2 instances:

```bash
# List all instances
aws ec2 describe-instances --query 'Reservations[].Instances[].[InstanceId,State.Name,Tags[0].Value]' --region us-east-1

# Terminate specific instance
aws ec2 terminate-instances --instance-ids i-XXXXX --region us-east-1

# Terminate all running instances (use with caution)
aws ec2 describe-instances --query 'Reservations[].Instances[?State.Name==`running`].InstanceId' --region us-east-1 | \
  xargs -I {} aws ec2 terminate-instances --instance-ids {} --region us-east-1
```

### 3. Delete Lambda Functions

```bash
aws lambda delete-function --function-name eks-ai-ops-toolkit --region us-east-1
aws lambda delete-function --function-name sre-slack-bot --region us-east-1
aws lambda delete-function --function-name sre-kubectl-helper --region us-east-1
```

### 4. Delete DynamoDB Tables

CloudFormation stacks with `DeletionPolicy: Retain` may leave tables behind:

```bash
aws dynamodb delete-table --table-name sre-incidents --region us-east-1
aws dynamodb delete-table --table-name sre-deployments --region us-east-1
```

### 5. Delete SSM Parameters

```bash
aws ssm delete-parameters --region us-east-1 --names \
  /eks-ai-ops-toolkit/slack-bot-token \
  /eks-ai-ops-toolkit/slack-signing-secret \
  /eks-ai-ops-toolkit/github-token \
  /eks-ai-ops-toolkit/anthropic-api-key \
  /eks-ai-ops-toolkit/mcp-gateway-url \
  /eks-ai-ops-toolkit/mcp-gateway-api-key \
  /eks-ai-ops-toolkit/eks-vpc-id \
  /eks-ai-ops-toolkit/eks-private-subnet-1 \
  /eks-ai-ops-toolkit/eks-private-subnet-2
```

### 6. Delete CloudWatch Log Groups

```bash
aws logs delete-log-group --log-group-name /aws/lambda/eks-ai-ops-toolkit --region us-east-1
aws logs delete-log-group --log-group-name /aws/lambda/sre-slack-bot --region us-east-1
aws logs delete-log-group --log-group-name /aws/lambda/sre-kubectl-helper --region us-east-1

# List all log groups to check for others
aws logs describe-log-groups --region us-east-1 --query 'logGroups[].logGroupName' | grep -i eks
```

### 7. Delete S3 Buckets

First, empty all buckets, then delete them:

```bash
# List all buckets
aws s3 ls

# Empty and delete
aws s3 rm s3://bucket-name --recursive --region us-east-1
aws s3 rb s3://bucket-name --region us-east-1

# Batch deletion
for bucket in $(aws s3 ls | awk '{print $3}'); do
  echo "Cleaning $bucket..."
  aws s3 rm s3://$bucket --recursive
  aws s3 rb s3://$bucket
done
```

### 8. Delete IAM Roles and Policies

```bash
# List roles related to this project
aws iam list-roles --query 'Roles[?contains(RoleName, `eks-ai-ops`) || contains(RoleName, `sre-`)].[RoleName]'

# Delete role inline policies first, then the role
aws iam delete-role-policy --role-name role-name --policy-name policy-name
aws iam delete-role --role-name role-name

# For roles with attached managed policies
aws iam list-attached-role-policies --role-name role-name
aws iam detach-role-policy --role-name role-name --policy-arn arn:aws:iam::...
aws iam delete-role --role-name role-name
```

### 9. Delete CloudWatch Alarms

```bash
aws cloudwatch delete-alarms --alarm-names eks-ai-ops-trigger --region us-east-1
```

### 10. Delete VPCs and VPC Resources

Only delete VPCs you explicitly created (not the default VPC):

```bash
# List non-default VPCs
aws ec2 describe-vpcs --query 'Vpcs[?IsDefault==`false`].[VpcId,Tags]' --region us-east-1

# Delete subnets, route tables, internet gateways, then VPC
aws ec2 delete-vpc --vpc-id vpc-XXXXX --region us-east-1
```

## Verification

After running cleanup, verify all resources have been removed:

```bash
# Check stacks
aws cloudformation list-stacks --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE --region us-east-1

# Check EC2 instances
aws ec2 describe-instances --query 'Reservations[].Instances[?State.Name==`running`]' --region us-east-1

# Check Lambda functions
aws lambda list-functions --region us-east-1

# Check DynamoDB tables
aws dynamodb list-tables --region us-east-1

# Check S3 buckets
aws s3 ls

# Check SSM parameters
aws ssm get-parameters-by-path --path /eks-ai-ops-toolkit --region us-east-1
```

## AWS CLI Authentication

If you need to authenticate or re-authenticate:

```bash
# Configure AWS CLI with credentials
aws configure

# Verify authentication
aws sts get-caller-identity

# Use specific profile
aws configure --profile profile-name
```

## Troubleshooting

### Stack Deletion Fails

**Issue**: "Stack cannot be deleted while TerminationProtection is enabled"

**Solution**:
```bash
aws cloudformation update-termination-protection --no-enable-termination-protection \
  --stack-name stack-name --region us-east-1
```

### Bucket Not Empty

**Issue**: Cannot delete bucket because it contains objects

**Solution**:
```bash
# First, remove all objects including versioned objects
aws s3 rm s3://bucket-name --recursive --region us-east-1

# For versioned buckets, also delete all versions
aws s3api delete-objects --bucket bucket-name --delete \
  "$(aws s3api list-object-versions --bucket bucket-name --query \
    'Versions[].{Key:Key,VersionId:VersionId}' | jq -r '.[] | {Key: .Key, VersionId: .VersionId}')"
```

### Dependency Conflicts

Some resources may have dependencies (e.g., EKS cluster requires subnets to be deleted first). CloudFormation typically handles this automatically, but if you get errors, delete in this order:

1. Lambda functions
2. DynamoDB tables
3. EC2 instances
4. Security groups
5. Subnets
6. Route tables
7. NAT gateways
8. Internet gateways
9. VPC

### Access Denied Errors

If you get permission errors:

1. Verify your AWS credentials: `aws sts get-caller-identity`
2. Ensure your IAM user has necessary permissions
3. Check if you're using the correct AWS profile: `aws configure list`
4. Try using a different region if resources were deployed elsewhere

## One-Command Full Cleanup

Combine all steps (use with caution):

```bash
#!/bin/bash
REGION=us-east-1

echo "Starting full AWS cleanup..."

# Delete stacks
aws cloudformation delete-stack --stack-name eks-ai-ops-toolkit --region $REGION
aws cloudformation delete-stack --stack-name aws-sam-cli-managed-default --region $REGION
aws cloudformation delete-stack --stack-name eksctl-eks-ai-ops-nodegroup-ng-632887e6 --region $REGION
aws cloudformation delete-stack --stack-name eksctl-eks-ai-ops-cluster --region $REGION
aws cloudformation delete-stack --stack-name smartdocextract --region $REGION

# Wait for stacks to delete
sleep 30

# Delete remaining resources
aws dynamodb delete-table --table-name sre-incidents --region $REGION 2>/dev/null || true
aws dynamodb delete-table --table-name sre-deployments --region $REGION 2>/dev/null || true

aws ssm delete-parameters --region $REGION --names \
  /eks-ai-ops-toolkit/slack-bot-token \
  /eks-ai-ops-toolkit/slack-signing-secret \
  /eks-ai-ops-toolkit/github-token \
  /eks-ai-ops-toolkit/anthropic-api-key 2>/dev/null || true

aws logs delete-log-group --log-group-name /aws/lambda/eks-ai-ops-toolkit --region $REGION 2>/dev/null || true
aws logs delete-log-group --log-group-name /aws/lambda/sre-slack-bot --region $REGION 2>/dev/null || true
aws logs delete-log-group --log-group-name /aws/lambda/sre-kubectl-helper --region $REGION 2>/dev/null || true

aws cloudwatch delete-alarms --alarm-names eks-ai-ops-trigger --region $REGION 2>/dev/null || true

echo "Cleanup initiated. Resources will be deleted over the next few minutes."
echo "Verify with: make verify-cleanup REGION=$REGION"
```

## Next Steps

After cleanup:

1. **Verify in AWS Console**: Log in to AWS Console and confirm resources are gone
2. **Check Billing**: Allow a few minutes for billing to update
3. **Remove Local State**: Delete samconfig.toml, .aws-sam directory, and other local build artifacts
4. **Update Credentials**: If credentials were stored locally, rotate them in AWS IAM

---

For more information, see [Makefile](./Makefile) and [step-by-step.md](./step-by-step.md).
