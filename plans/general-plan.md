Project: Medicare Fraud Detection — AWS Data Pipeline & ML System

Project Overview Build an end-to-end AWS data pipeline that ingests public CMS
Medicare claims data, processes it through Glue and Athena, trains an anomaly
detection model to flag potentially fraudulent providers, and visualises results
in QuickSight. The output is a working product demonstrating AWS data
engineering skills and a potential startup proof-of-concept.

Data Sources All publicly available, no registration required:

  - CMS Medicare Provider Utilization and Payment Data:
    https://data.cms.gov/provider-summary-by-type-of-service
  - CMS Physician and Other Supplier Data (Part B)
  - CMS Medicare Part D Prescriber Data
  - CMS Open Payments (financial relationships between pharma and doctors)

Start with Physician and Other Supplier Part B — it contains:

  - Provider NPI, name, specialty, location
  - Procedure codes (HCPCS)
  - Number of services rendered
  - Total Medicare payments
  - Average payment per service

This is the richest dataset for fraud detection.

Architecture

CMS Public Data (CSV downloads)
        ↓
AWS S3 — Raw Zone (bronze)
(store original CSVs as-is)
        ↓
AWS Glue ETL Job — Python/PySpark
(clean, structure, feature engineering)
        ↓
AWS S3 — Processed Zone (silver)
(parquet format, partitioned by year/specialty)
        ↓
AWS Glue Data Catalog
(register schema, make queryable)
        ↓
Amazon Athena
(SQL analytics on processed data)
        ↓
AWS S3 — Analytics Zone (gold)
(aggregated features for ML)
        ↓
Amazon SageMaker
(train anomaly detection model)
        ↓
AWS Lambda + API Gateway
(serve fraud score predictions via REST API)
        ↓
Amazon QuickSight
(dashboard — flagged providers, geographic heatmap, top anomalies)

Phase 1 — Data Ingestion (Week 1)

Step 1: Set up AWS infrastructure

# Create S3 buckets
aws s3 mb s3://medicare-fraud-raw
aws s3 mb s3://medicare-fraud-processed  
aws s3 mb s3://medicare-fraud-analytics

# Set up IAM roles
# Glue role: S3 read/write, Glue full access
# SageMaker role: S3 read/write, SageMaker full access
# Lambda role: S3 read, invoke SageMaker endpoint

Step 2: Download and upload CMS data

import boto3
import requests

# Download CMS Part B data
url = "https://data.cms.gov/provider-summary-by-type-of-service/medicare-physician-other-practitioners/medicare-physician-other-practitioners-by-provider-and-service/api/1/datastore/query"

# Upload to S3 raw zone
s3 = boto3.client('s3')
s3.upload_file(
    'medicare_part_b_2022.csv',
    'medicare-fraud-raw',
    'part_b/2022/medicare_part_b_2022.csv'
)

Phase 2 — Glue ETL Pipeline (Week 1-2)

Glue Job — Clean and feature engineer:

import sys
from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from awsglue.context import GlueContext
from pyspark.sql import functions as F
from pyspark.sql.window import Window

sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session

# Read raw data
df = spark.read.csv(
    "s3://medicare-fraud-raw/part_b/2022/",
    header=True,
    inferSchema=True
)

# Clean
df = df.dropDuplicates(['npi', 'hcpcs_code'])
df = df.filter(df.total_medicare_payment_amt > 0)

# Feature engineering — fraud signals
window_spec = Window.partitionBy('provider_type')

df = df.withColumn(
    # How many services per provider vs specialty average
    'services_vs_specialty_avg',
    df.tot_srvcs / F.avg('tot_srvcs').over(window_spec)
).withColumn(
    # Payment per service vs specialty average  
    'payment_per_service',
    df.total_medicare_payment_amt / df.tot_srvcs
).withColumn(
    'payment_vs_specialty_avg',
    F.col('payment_per_service') / F.avg('payment_per_service').over(window_spec)
).withColumn(
    # Unique patients vs services ratio — low ratio = suspicious
    'patient_service_ratio',
    df.bene_unique_cnt / df.tot_srvcs
).withColumn(
    # Geographic outlier flag
    'state',
    df.nppes_provider_state
)

# Write to processed zone as parquet
df.write.mode('overwrite').partitionBy('provider_type').parquet(
    "s3://medicare-fraud-processed/part_b/2022/"
)

Register in Glue Data Catalog:

# Create Glue crawler to register schema
# Run via AWS console or CLI
aws glue create-crawler \
    --name medicare-processed-crawler \
    --role GlueRole \
    --database-name medicare_db \
    --targets '{"S3Targets": [{"Path": "s3://medicare-fraud-processed/"}]}'

aws glue start-crawler --name medicare-processed-crawler

Phase 3 — Athena Analytics (Week 2)

Key SQL queries to build:

-- Top providers by services rendered vs specialty average
SELECT 
    npi,
    nppes_provider_last_org_name,
    provider_type,
    nppes_provider_state,
    tot_srvcs,
    services_vs_specialty_avg,
    payment_per_service,
    payment_vs_specialty_avg,
    patient_service_ratio
FROM medicare_db.part_b
WHERE services_vs_specialty_avg > 3  -- 3x specialty average
    AND payment_vs_specialty_avg > 2  -- paying 2x more than peers
ORDER BY services_vs_specialty_avg DESC
LIMIT 100;

-- Suspicious providers: high services, low unique patients
SELECT
    npi,
    nppes_provider_last_org_name,
    provider_type,
    tot_srvcs,
    bene_unique_cnt,
    patient_service_ratio,
    total_medicare_payment_amt
FROM medicare_db.part_b
WHERE patient_service_ratio < 0.3  -- seeing same patients repeatedly
    AND tot_srvcs > 1000
ORDER BY patient_service_ratio ASC;

-- Geographic hotspots — states with highest payment anomalies
SELECT
    nppes_provider_state,
    COUNT(DISTINCT npi) as provider_count,
    AVG(payment_vs_specialty_avg) as avg_payment_anomaly,
    SUM(total_medicare_payment_amt) as total_payments
FROM medicare_db.part_b
GROUP BY nppes_provider_state
ORDER BY avg_payment_anomaly DESC;

Phase 4 — ML Anomaly Detection on SageMaker (Week 2-3)

Train Isolation Forest model:

import boto3
import pandas as pd
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler
import joblib

# Load aggregated features from S3
s3 = boto3.client('s3')
df = pd.read_parquet('s3://medicare-fraud-analytics/features/provider_features.parquet')

# Select fraud signal features
features = [
    'services_vs_specialty_avg',
    'payment_vs_specialty_avg', 
    'patient_service_ratio',
    'tot_srvcs',
    'payment_per_service',
    'bene_unique_cnt'
]

X = df[features].fillna(0)

# Scale features
scaler = StandardScaler()
X_scaled = scaler.fit_transform(X)

# Train Isolation Forest
model = IsolationForest(
    contamination=0.05,  # expect 5% fraudulent providers
    n_estimators=200,
    random_state=42
)

model.fit(X_scaled)

# Generate fraud scores
df['anomaly_score'] = model.score_samples(X_scaled)
df['is_fraudulent'] = model.predict(X_scaled) == -1

# Save model artifacts
joblib.dump(model, 'model.joblib')
joblib.dump(scaler, 'scaler.joblib')

# Upload to S3 for SageMaker
s3.upload_file('model.joblib', 'medicare-fraud-analytics', 'model/model.joblib')
s3.upload_file('scaler.joblib', 'medicare-fraud-analytics', 'model/scaler.joblib')

# Write results back
df.to_parquet('s3://medicare-fraud-analytics/predictions/fraud_scores.parquet')

Deploy as SageMaker endpoint:

import sagemaker
from sagemaker.sklearn import SKLearnModel

sagemaker_session = sagemaker.Session()

model = SKLearnModel(
    model_data='s3://medicare-fraud-analytics/model/model.tar.gz',
    role='SageMakerRole',
    framework_version='1.0-1',
    entry_point='inference.py'
)

predictor = model.deploy(
    initial_instance_count=1,
    instance_type='ml.t2.medium'
)

Phase 5 — Lambda API (Week 3)

import json
import boto3
import numpy as np

sagemaker_runtime = boto3.client('sagemaker-runtime')

def lambda_handler(event, context):
    # Parse provider features from request
    body = json.loads(event['body'])
    
    features = [
        body['services_vs_specialty_avg'],
        body['payment_vs_specialty_avg'],
        body['patient_service_ratio'],
        body['tot_srvcs'],
        body['payment_per_service'],
        body['bene_unique_cnt']
    ]
    
    # Call SageMaker endpoint
    response = sagemaker_runtime.invoke_endpoint(
        EndpointName='medicare-fraud-detector',
        ContentType='application/json',
        Body=json.dumps({'features': features})
    )
    
    result = json.loads(response['Body'].read())
    
    return {
        'statusCode': 200,
        'body': json.dumps({
            'npi': body['npi'],
            'fraud_score': result['anomaly_score'],
            'is_flagged': result['is_fraudulent'],
            'risk_level': 'HIGH' if result['anomaly_score'] < -0.3 else 'MEDIUM' if result['anomaly_score'] < -0.1 else 'LOW'
        })
    }

Phase 6 — QuickSight Dashboard (Week 3-4)

Connect QuickSight to Athena and build:

  - Executive summary: total providers analysed, % flagged, total suspicious
    payments
  - Geographic heatmap: fraud risk by US state
  - Top 20 flagged providers: ranked by anomaly score with drill-down
  - Specialty breakdown: which medical specialties have highest anomaly rates
  - Trend analysis: year-over-year change in fraud patterns

MLflow tracking (optional but adds credibility):

import mlflow

with mlflow.start_run(run_name="isolation_forest_v1"):
    mlflow.log_param("contamination", 0.05)
    mlflow.log_param("n_estimators", 200)
    mlflow.log_metric("flagged_providers_pct", 5.2)
    mlflow.log_metric("avg_anomaly_score_flagged", -0.42)
    mlflow.sklearn.log_model(model, "fraud_model")

Tech stack covered by this project:

  - S3 — data lake storage
  - AWS Glue — ETL and data catalog
  - Athena — SQL analytics
  - SageMaker — ML training and deployment
  - Lambda + API Gateway — serverless API
  - QuickSight — BI dashboard
  - IAM — roles and permissions
  - CloudWatch — monitoring and logging

Timeline:

  - Week 1: S3 setup, data ingestion, Glue ETL
  - Week 2: Athena queries, feature engineering, initial anomaly detection
  - Week 3: SageMaker training and deployment, Lambda API
  - Week 4: QuickSight dashboard, documentation, GitHub README

GitHub README should include:

  - Architecture diagram
  - Problem statement
  - Dataset description
  - Key findings — top flagged providers, suspicious patterns found
  - Tech stack
  - How to run locally
  - Link to live QuickSight dashboard (if public)

Startup angle to develop further: Once the pipeline works, the product extension
is:

  - Add Open Payments data — flag doctors receiving large pharma payments AND
    billing anomalously
  - Add Part D prescriber data — flag unusual opioid prescribing patterns
  - Build a simple web UI for insurance companies to query provider risk scores
  - Monetise: sell API access to insurance companies, healthcare law firms,
    hospital compliance teams

lets focus on this

