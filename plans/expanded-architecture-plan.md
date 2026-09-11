# Expanded Architecture Plan: Medicare Fraud Detection

This document outlines the expanded architecture for the Medicare Fraud Detection project, upgrading the pipeline from a baseline Data Lake/ML setup to an enterprise-grade, event-driven, containerized application with GenAI capabilities and Infrastructure as Code (IaC).

## 🗺️ Visual Architecture Diagram

```text
                                [ CMS Public Data ]
                                         │
                                         ▼ (Manual or Event-Driven Upload)
                             [ AWS S3 - Raw (Bronze) ]
                                         │
                                         │ (S3 Put Event)
                                         ▼
                                [ AWS EventBridge ]
                                         │
                                         │ (Triggers Execution)
                                         ▼
                             [ AWS Step Functions ] (Workflow Orchestrator)
                                  │           │
                     ┌────────────┘           └────────────┐
                     ▼ (Step 1: ETL)                       ▼ (Step 2: Train & Predict)
           [ AWS Glue ETL Job ]                  [ Amazon SageMaker ]
                     │ (PySpark)                           │
                     ▼                                     ▼ (Model & Real-time Predictions)
        [ AWS S3 - Processed (Silver) ]          [ Model Artifacts & Endpoint ]
                     │                                     │
                     ├───► [ AWS Glue Data Catalog ]       │
                     │                                     ▼
                     ▼                                [ AWS Lambda ] (Anomaly Processor)
             [ Amazon Athena ]                             │
                     │ (SQL Analytics)                     ├────────────────┐
                     ▼                                     ▼                ▼
         [ AWS S3 - Analytics (Gold) ]              [ Amazon Bedrock ]      │ (Saves raw scores)
                     │                                (LLM GenAI            │
                     │ (Source for SageMaker)          Analysis)            │
                     └─────────────────────────────────────┬────────────────┘
                                                           │
                                                           ▼ (Provider Risk Profiles & Summaries)
                                                  [ Amazon DynamoDB ]
                                                           ▲
                                                           │ (Reads Fast Cache)
                     [ AWS CloudWatch ] ◄───────── [ Amazon ECS (Fargate) ] ◄──► [ Amazon RDS (PostgreSQL) ]
                    (Centralized Logs)                     ▲       ▲               (Relational Investigator State,
                                                           │       │               Audit Logs & User Accounts)
                                                           │   [ Amazon ECR ] (Stores API Image)
                                                           │
                                                           ▼ (Secure API Delivery)
                                                 [ Amazon API Gateway ]
                                                           ▲
                                                           │ (Investigator Requests)
                                                     [ End User ]
                                                           ▲
                                                           │ (Dashboard Visualization)
                                                 [ Amazon QuickSight ]
                                                           │
                                                           ▼ (Interactive Queries)
                                                    [ Amazon Athena ]
```

## 🏗️ Expanded Architecture Map

### 1. Infrastructure as Code (IaC) & CI/CD
*   **Tool**: **Terraform** (or AWS CDK)
*   **Role**: Fully codify the deployment of S3 buckets, IAM roles, Glue jobs, VPCs, ECS clusters, RDS instances, and Step Functions. Manual AWS CLI commands will be replaced by reusable, declarative modules.

### 2. Orchestration & Event-Driven Ingestion
*   **AWS EventBridge**: Monitors the S3 Raw bucket for new CMS data uploads. When a new file lands, it triggers the Step Functions state machine.
*   **AWS Step Functions**: Replaces manual/cron execution. Orchestrates the entire pipeline natively:
    1.  Triggers AWS Glue ETL.
    2.  Waits for Glue completion.
    3.  Triggers Amazon SageMaker training.
    4.  Updates the Model Registry.
    5.  Triggers an ECS task to update DynamoDB caching.

### 3. Data Processing & Analytics (Existing + CloudWatch)
*   **AWS S3**: Bronze/Silver/Gold data lake storage.
*   **AWS Glue**: ETL processing, converting CSV to partitioned Parquet, feature engineering.
*   **Amazon Athena**: Serverless SQL querying.
*   **AWS CloudWatch**: Centralized logging for Glue jobs, Step Functions state transitions, ECS containers, and API metrics. Set up CloudWatch Alarms for pipeline failures.

### 4. Machine Learning & Agentic Logic
*   **Amazon SageMaker**: Trains the Isolation Forest model and hosts the real-time inference endpoint.
*   **Amazon Bedrock**: Introduces Agentic Business Logic.
    *   *Use Case*: When SageMaker flags a provider as high-risk, a Lambda function calls Bedrock (e.g., Claude 3) with the provider's feature set and peer averages. Bedrock generates a human-readable "Investigation Summary" (e.g., *"Dr. Smith is flagged due to billing 400% more HCPCS code 99214 than the state average for Cardiology"*).

### 5. Backend Serving & Containerization
*   **Amazon ECR**: Stores the Docker images for the backend web API.
*   **Amazon ECS (Fargate)**: Runs the core backend application (e.g., a Python FastAPI or Node.js Express app). This service handles complex business logic, user authentication, and serves the frontend.
*   **Amazon API Gateway**: Fronts the ECS cluster (via VPC Link) and Lambda functions, providing rate limiting, authentication, and a unified RESTful interface.

### 6. Operational Databases
*   **Amazon DynamoDB (NoSQL)**: High-speed caching and lookup. Stores the latest ML risk scores and Bedrock-generated summaries for every provider. Allows the API to fetch a provider's fraud risk in milliseconds without querying Athena or invoking SageMaker.
*   **Amazon RDS (PostgreSQL)**: Relational data storage for the application state.
    *   *Use Case*: Stores investigator user accounts, audit logs of who reviewed which fraud case, manual overrides, and historical tracking of investigation statuses (e.g., "Pending", "Reviewed", "Reported to CMS").

## 🔄 Data Flow Summary

1.  **Ingest**: Admin uploads CMS data to S3.
2.  **Orchestrate**: EventBridge detects upload -> triggers Step Functions.
3.  **Process**: Step Functions runs Glue (S3 Bronze -> Silver -> Gold).
4.  **Train & Score**: Step Functions triggers SageMaker.
5.  **GenAI Augmentation**: Lambda processes anomalies, calling Bedrock to generate human-readable explanations.
6.  **Store**: Scores and Bedrock summaries are saved to DynamoDB.
7.  **Serve**: User requests a provider profile via API Gateway -> ECS container fetches fast data from DynamoDB and investigator state from RDS.

## 🚀 Next Steps for Implementation

1.  **Phase 1: IaC Foundation**: Initialise a Terraform module to manage the existing S3, IAM, and Glue resources.
2.  **Phase 2: Orchestration**: Wrap the Glue jobs in a Step Functions state machine triggered by EventBridge.
3.  **Phase 3: Database & GenAI**: Spin up DynamoDB and RDS. Implement the Bedrock Lambda trigger for flagged anomalies.
4.  **Phase 4: Containerization**: Build the FastAPI backend, push to ECR, and deploy on ECS Fargate behind API Gateway.
