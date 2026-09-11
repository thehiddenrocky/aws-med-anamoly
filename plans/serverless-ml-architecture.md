# Serverless ML Architecture Plan: Quota-Independent Medicare Fraud Pipeline

This plan details an updated enterprise-grade, event-driven architecture for the Medicare Fraud Detection system. It retains the robust data lake, ETL orchestration, operational database, and GenAI capabilities of our expanded roadmap, but introduces a major pivot: **fully replacing Amazon SageMaker with a serverless Scikit-Learn + AWS Lambda + S3 pipeline**.

---

## 💡 Why Skip Amazon SageMaker? (Forensic & Financial Rationale)

In sandbox or restricted AWS accounts, standard and spot training instances (such as `ml.m5.large`) are often default-throttled to **0** by AWS Service Quotas, requiring support cases and multi-day approval delays. 

Additionally, running active, persistent SageMaker real-time endpoints can cost **$50 to $150+ per month** in idle execution fees. 

By pivoting to a **Serverless ML Serving Pattern**, we achieve:
1. **Instant Quota Independence**: No waiting for AWS Service Quotas; everything runs on standard AWS Lambda CPU.
2. **True Zero-Idle Cost**: Lambda only bills per invocation (fractions of a cent). If no one queries the API, the ML serving layer costs exactly **$0.00**.
3. **High Performance**: Serving a pre-trained Scikit-Learn Isolation Forest model via Lambda runs in **under 20 milliseconds**, fully matching the latency of a dedicated SageMaker endpoint.
4. **Architectural Simplicity**: Avoids configuring complex SageMaker custom Docker containers, Model Registries, and endpoint configurations, allowing us to focus on data engineering and business value.

---

## 🗺️ Visual Serverless Architecture Diagram

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
                                         │ (Triggers State Machine)
                                         ▼
                             [ AWS Step Functions ] (Workflow Orchestrator)
                                  │           │
                     ┌────────────┘           └────────────┐
                     ▼ (Step 1: ETL)                       ▼ (Step 2: Serverless ML Training)
           [ AWS Glue ETL Job ]                     [ AWS Lambda / Glue Python Shell ]
                     │ (PySpark)                           │ (Fits Isolation Forest & Scaler)
                     ▼                                     ▼
        [ AWS S3 - Processed (Silver) ]             [ AWS S3 - Analytics (Gold) ]
                     │                                     │  (Stores model.joblib &
                     ├───► [ AWS Glue Data Catalog ]       │   scaler.joblib artifacts)
                     │                                     │
                     ▼                                     ▼ (Reads Models on Cold Start)
             [ Amazon Athena ]                      [ AWS Lambda - Serving ] (Inference Engine)
                     │ (SQL Analytics)                     │
                     ▼                                     ├────────────────┐
         [ AWS S3 - Analytics (Gold) ]                     ▼                ▼
                     │ (Feature Store Table)         [ Amazon Bedrock ]     │ (Saves predictions)
                     └─────────────────────────────►   (Claude LLM          │
                                                        Summary)            │
                                                           │                │
                                                           ▼ (Risk Profile)  ▼ (Inference CSV)
                                                  [ Amazon DynamoDB ] ◄─────┘
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

---

## 🏗️ Detailed Component Map

### 1. Ingestion & Analytical Data Lake (Existing S3 + Glue + Athena)
* **S3 Buckets**: 
  * Bronze: `s3://medicare-fraud-raw-023413058557/` (Raw CSV)
  * Silver: `s3://medicare-fraud-processed-023413058557/` (Partitioned Parquet)
  * Gold: `s3://medicare-fraud-analytics-023413058557/` (Aggregated Provider Features)
* **AWS Glue ETL & Catalog**: Cleans claims, joins with beneficiary details, and aggregates provider-level forensic metrics using PySpark. Registers structures in the Glue Catalog database (`medicare_db`).
* **Amazon Athena**: Runs serverless SQL analytics and executes the feature-store generation query (CTAS) to update the Gold zone.

### 2. Serverless ML Artifact & Model Registry (S3-based)
Instead of a dedicated SageMaker Model Registry, we use S3 versioning as our artifact store:
* **Storage Location**: `s3://medicare-fraud-analytics-023413058557/models/`
* **Artifacts**: 
  * `scaler.joblib`: Enforces mathematical normalization consistency during inference.
  * `model.joblib`: Serialized Isolation Forest model.
* **Serverless Training Engine**: Training can be automated via a lightweight **AWS Glue Python Shell Job** or a dedicated **AWS Lambda function** triggered whenever Gold features are updated. It fits the model using Scikit-Learn, serializes artifacts, and pushes them to S3.

### 3. Serverless Inference & Real-time Serving Layer (API Gateway + Lambda)
* **Amazon API Gateway**: Receives secure HTTP REST POST requests from end-users or upstream services (e.g., `POST /assess-provider`).
* **AWS Lambda (Inference Engine)**:
  * On a "cold start", Lambda downloads `scaler.joblib` and `model.joblib` from S3 into its high-speed in-memory `/tmp` space.
  * For subsequent "warm" requests, it keeps the model loaded in RAM, achieving sub-millisecond scoring execution.
  * Receives the raw 7 numerical features of a provider, scales them using the scaler, evaluates them through the Isolation Forest, and outputs:
    * `anomaly_score`: Continuous outlier measure.
    * `is_anomaly`: Boolean fraud flag.
    * `risk_level`: Severity class (`HIGH`, `MEDIUM`, or `LOW`).

### 4. GenAI Explanation Layer (Amazon Bedrock)
* **Amazon Bedrock (Claude 3.5 Sonnet / Haiku)**: Connects directly to our anomaly detection pipeline.
* **Workflow**: When the serving Lambda identifies a `HIGH` risk provider:
  1. It fetches the provider's feature values and the average peer features for that provider's region/specialty.
  2. It structures an event and queries Bedrock.
  3. Bedrock generates a highly descriptive, professional forensic narrative explaining *why* the provider was flagged (e.g., *"Provider PRV53033 is flagged in the 99th percentile due to an extreme maximum daily reimbursement of $36,500.00 while only servicing 1 unique patient"*).
  4. The summary is stored directly alongside the score.

### 5. Backend Serving, Caching, and Core Application
* **Amazon DynamoDB (NoSQL)**: Ultra-fast cache database storing pre-calculated provider profiles, risk scores, and Bedrock narratives. This allows the web API to serve instant queries without touching S3 or hitting an active ML server.
* **Amazon RDS (PostgreSQL)**: Relational storage of investigator states. Manages user logs, investigator review histories (e.g., marking a flagged provider as "In Progress", "Verified Fraud", or "Whitelisted"), and system audit trails.
* **Amazon ECS (Fargate) / API Gateway**: A Dockerized FastAPI backend application deployed on serverless container infrastructure to handle user login, role management, and structured investigator queries.

---

## 🔄 Dynamic Pipeline Data Flow

1. **Ingestion**: Raw claims CSV are uploaded to Bronze S3.
2. **ETL & Catalog**: EventBridge triggers Step Functions, running the Glue Spark ETL, writing Silver Parquet partitions, and updating the Glue Catalog.
3. **Gold Generation**: Athena executes a CTAS query to compile the provider-level ML feature store in S3 Gold.
4. **Serverless Training**: A Glue Python Shell job trains Scikit-Learn Isolation Forest on the Gold features and writes updated `model.joblib`, `scaler.joblib`, and `anomaly_predictions.parquet` to S3.
5. **Caching & GenAI**: An AWS Lambda function is triggered by the new predictions parquet file:
   * It loads the predictions into **DynamoDB**.
   * If a provider's risk is `HIGH`, it invokes **Amazon Bedrock** to append a natural language explanation, saving it directly to DynamoDB.
6. **Real-time Serving**: An investigator requests `GET /provider/PRV53033` via API Gateway. ECS Fargate instantly serves the ML score, raw features, and GenAI forensic summary directly from DynamoDB in < 50 milliseconds.

---

## 🚀 Iterative Phase-by-Phase Transition Plan

We will proceed with the implementation using this highly optimized, serverless design:

### 📍 Phase 1: Local Model & Gold Feature Baseline (COMPLETED)
* ✅ Curate 7 forensic billing features from Silver claims data.
* ✅ Train local Scikit-Learn Isolation Forest and serialize artifacts (`model.joblib`, `scaler.joblib`).
* ✅ Verify that known targets (`PRV53033`, `PRV52627`, `PRV53471`) are accurately scored and flagged in the top 5% anomalies.

### 📍 Phase 2: Deploy Serverless Model Store (Next Step)
* ◽ Upload pre-trained `model.joblib` and `scaler.joblib` to S3 under `s3://medicare-fraud-analytics-023413058557/models/`.
* ◽ Package a lightweight Lambda Deployment ZIP containing `scikit-learn`, `pandas`, and `joblib`.
* ◽ Implement AWS Lambda inference logic to download models from S3 on startup and score request payloads.

### 📍 Phase 3: Setup Fast Cache & NoSQL (DynamoDB Setup)
* ◽ Create a DynamoDB table `medicare_provider_scores` (Hash Key: `provider_id`).
* ◽ Implement a Lambda script to bulk-load predictions from `anomaly_predictions.parquet` into DynamoDB.
* ◽ Query the DynamoDB table directly via AWS CLI to verify sub-millisecond retrieval.

### 📍 Phase 4: Serverless API Gateway Deployment
* ◽ Provision a serverless Amazon API Gateway REST API.
* ◽ Create a `POST /assess-provider` endpoint pointing to our scoring Lambda.
* ◽ Perform API testing using local client requests to retrieve real-time risk scores on-demand.

### 📍 Phase 5: Integrate GenAI & Athena Dashboards
* ◽ Write the Bedrock Lambda trigger that compiles feature metrics and generates Claude summaries for high-risk anomalies.
* ◽ Create QuickSight dashboards sourcing directly from Athena to visualize geographic hotspots and provider risk distributions.
