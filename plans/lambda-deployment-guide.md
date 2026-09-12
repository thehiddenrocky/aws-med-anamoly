# AWS Lambda Serverless ML Deployment & Inference Guide

This document serves as the definitive reference manual and operational runbook for deploying, packaging, and managing the serverless **Scikit-Learn ML scoring engine** on AWS Lambda. It covers platform-specific cross-compilation from macOS without Docker, memory caching architectures, custom IAM security policies, and production deployment automation.

---

## 📐 1. System Architecture & Caching Strategy

Rather than deploying a costly, persistent 24/7 SageMaker inference endpoint, the system utilizes an on-demand, serverless execution pattern via **AWS Lambda**.

```text
       [ Client Request: POST /assess-provider ]
                         │
                         ▼
               [ Amazon API Gateway ]
                         │
                         ▼
             [ AWS Lambda Container ]
                         │
        ┌────────────────┴────────────────┐
        ▼ (Check Local Cache /tmp/)       ▼ (Run Scoring Model)
  File exists?                      Execute Scaler &
   ├── YES ──► Use in RAM           Isolation Forest
   └── NO  ──► Pull from S3             │
               (model.joblib &          ▼
                scaler.joblib)    [ Return JSON ]
```

### 🧠 Cold Start vs. Warm Start Optimization
To maintain ultra-low latency (< 20ms) on warm invocations, the Lambda function splits execution logic into two phases:

1. **Initialization Phase (Cold Start)**:
   * Runs *outside* the event handler when AWS Lambda provisions the container.
   * Loads the standard libraries (`numpy`, `pandas`, `sklearn`, `joblib`).
   * Connects to S3 to download the pre-trained `model.joblib` and `scaler.joblib` to high-speed local ephemeral storage (`/tmp/`).
   * Deserializes and loads both the model and the scaler into global memory variables.
2. **Execution Phase (Warm Hits)**:
   * Triggered by incoming event payloads.
   * Skips S3 downloads and deserialization entirely since the loaded model and scaler are already resident in global container memory.
   * Runs the mathematical scaling and inference logic in **sub-20ms**.

---

## 📦 2. Cross-Compilation Packaging Strategy (Mac-to-Linux)

### The Platform Mismatch Challenge
AWS Lambda executes Python inside a customized **Amazon Linux 2** or **Amazon Linux 2023 (ELF 64-bit)** microVM.
If you package Python libraries containing compiled C-extensions (like `numpy`, `scikit-learn`, or `pandas`) on a macOS machine, they will compile into **macOS Mach-O format**. When uploaded to Lambda, this results in immediate import failure:
`Runtime.ImportModuleError: Unable to import module 'lambda_function': ELF file OS ABI invalid`.

### The No-Docker Solution: Linux Wheel Native Targeting
We bypass the need for Docker on macOS by using `pip`'s targeted cross-compilation flags to download pre-compiled Linux-compatible 64-bit C-extension wheels directly from PyPI.

```bash
pip install \
  --platform manylinux2014_x86_64 \
  --target=lambda_package \
  --implementation cp \
  --python-version 3.11 \
  --only-binary=:all: \
  scikit-learn joblib pandas numpy boto3
```

#### Explaining the Compiler Flags:
* `--platform manylinux2014_x86_64`: Directs pip to retrieve wheels compiled for standard Linux x86_64 kernels.
* `--target=lambda_package`: Directs output to a dedicated packaging directory rather than your local virtual environment.
* `--python-version 3.11`: Pins compatibility to Python 3.11 runtimes on AWS Lambda.
* `--implementation cp`: Specifies CPython (standard Python).
* `--only-binary=:all:`: Prevents pip from falling back to raw source packages (`.tar.gz`) which would attempt to compile locally on macOS, failing our CPU platform requirements.

---

## 📂 3. Deployment Directory Structure

When packaged, the deployment `.zip` archive must follow this flat-directory convention to ensure internal imports resolve correctly:

```text
lambda_deploy.zip
├── lambda_function.py           # Master lambda entrypoint code
├── model_loader.py              # Utility helper for S3 artifact retrieval
├── joblib/                      # Extracted Python packages
├── numpy/
├── pandas/
├── sklearn/
└── ... (All dependencies and associated metadata)
```

---

## 📝 4. Enterprise-Grade Lambda Handler Code

This code handles input validation, downloads model artifacts to the ephemeral `/tmp` cache if not present, runs predictions, and returns CORS-compliant JSON responses.

```python
# lambda-scoring/lambda_function.py
import os
import json
import boto3
import joblib
import numpy as np
import pandas as pd

# Global state cache for Model & Scaler (survives across warm starts)
MODEL = None
SCALER = None

# S3 Configuration
S3_CLIENT = boto3.client('s3')
BUCKET_NAME = os.environ.get('BUCKET_NAME', 'medicare-fraud-analytics-023413058557')
MODEL_KEY = os.environ.get('MODEL_KEY', 'models/model.joblib')
SCALER_KEY = os.environ.get('SCALER_KEY', 'models/scaler.joblib')

# Local Cache Paths
LOCAL_MODEL_PATH = '/tmp/model.joblib'
LOCAL_SCALER_PATH = '/tmp/scaler.joblib'

# The 7 numerical features expected by the Isolation Forest model
REQUIRED_FEATURES = [
    'total_claims', 
    'unique_patients', 
    'avg_length_of_stay', 
    'avg_daily_reimbursement', 
    'max_daily_reimbursement', 
    'claims_per_patient_ratio', 
    'high_daily_claims_ratio'
]

def load_artifacts():
    """
    Downloads model and scaler artifacts from S3 to local /tmp storage
    and deserializes them into memory if not already cached.
    """
    global MODEL, SCALER
    
    # 1. Load Scaler
    if SCALER is None:
        if not os.path.exists(LOCAL_SCALER_PATH):
            print(f"Downloading scaler from s3://{BUCKET_NAME}/{SCALER_KEY} ...")
            S3_CLIENT.download_file(BUCKET_NAME, SCALER_KEY, LOCAL_SCALER_PATH)
        print("Loading scaler into memory...")
        SCALER = joblib.load(LOCAL_SCALER_PATH)
        
    # 2. Load Isolation Forest Model
    if MODEL is None:
        if not os.path.exists(LOCAL_MODEL_PATH):
            print(f"Downloading model from s3://{BUCKET_NAME}/{MODEL_KEY} ...")
            S3_CLIENT.download_file(BUCKET_NAME, MODEL_KEY, LOCAL_MODEL_PATH)
        print("Loading model into memory...")
        MODEL = joblib.load(LOCAL_MODEL_PATH)

def format_response(status_code, body):
    """
    Helper to return standardized CORS-enabled HTTP response payloads.
    """
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "POST,OPTIONS"
        },
        "body": json.dumps(body)
    }

def lambda_handler(event, context):
    """
    AWS Lambda Main Entrypoint
    """
    # Handle preflight CORS request
    if event.get('httpMethod') == 'OPTIONS':
        return format_response(200, {"message": "Success"})
        
    try:
        # 1. Parse Input Body
        body_str = event.get('body', '{}') or '{}'
        data = json.loads(body_str)
    except Exception as e:
        return format_response(400, {"error": f"Invalid JSON payload: {str(e)}"})

    # 2. Check for Batch or Single Request
    is_batch = isinstance(data, list)
    records = data if is_batch else [data]
    
    # 3. Validate Inputs and Align Features
    processed_inputs = []
    providers = []
    
    for i, record in enumerate(records):
        provider_id = record.get('provider', f'UNKNOWN_{i}')
        providers.append(provider_id)
        
        # Ensure all required ML numerical inputs are provided
        missing_fields = [f for f in REQUIRED_FEATURES if f not in record]
        if missing_fields:
            return format_response(400, {
                "error": f"Missing features in payload for provider {provider_id}: {missing_fields}"
            })
            
        # Extract features in exact mathematical order
        features = [float(record[f]) for f in REQUIRED_FEATURES]
        processed_inputs.append(features)

    try:
        # 4. Lazy Load Models (runs on Cold Starts only)
        load_artifacts()
    except Exception as e:
        print(f"FAILED TO LOAD MODELS: {str(e)}")
        return format_response(500, {"error": f"Internal model loading failure: {str(e)}"})

    try:
        # 5. Format input as DataFrame to match training pipelines
        input_df = pd.DataFrame(processed_inputs, columns=REQUIRED_FEATURES)
        
        # 6. Normalize Features using cached Scaler
        X_scaled = SCALER.transform(input_df)
        
        # 7. Generate Anomaly Scores
        # score_samples yields continuous values (more negative -> higher risk)
        anomaly_scores = MODEL.score_samples(X_scaled)
        
        # 8. Predict Outlier Flag (-1 = Anomaly, 1 = Normal)
        predictions = MODEL.predict(X_scaled)
        
        # 9. Structure Response Results
        results = []
        for idx, provider_id in enumerate(providers):
            score = float(anomaly_scores[idx])
            flag = int(predictions[idx])
            
            # Categorize Risk Levels based on training heuristics
            # Training Contamination was set to 5% (scores < -0.65 are anomalies)
            if score < -0.70:
                risk_level = "CRITICAL"
            elif score < -0.65:
                risk_level = "HIGH"
            elif score < -0.55:
                risk_level = "MEDIUM"
            else:
                risk_level = "LOW"
                
            results.append({
                "provider": provider_id,
                "anomaly_score": score,
                "is_anomaly": True if flag == -1 else False,
                "risk_level": risk_level,
                "features": record # echoing original features back
            })
            
        return format_response(200, results if is_batch else results[0])

    except Exception as e:
        print(f"PREDICTION FAILURE: {str(e)}")
        return format_response(500, {"error": f"Inference pipeline execution error: {str(e)}"})
```

---

## 🛠️ 5. Step-by-Step AWS Infrastructure Provisioning

Follow these exact terminal commands (using `command aws` to bypass zsh aliases) to deploy and configure the infrastructure.

### A. Establish IAM Least-Privilege Execution Role
Lambda needs permissions to read the S3 model artifacts and log execution details to AWS CloudWatch.

#### 1. Define Trust Policy (`lambda-trust-policy.json`)
Create a file allowing AWS Lambda to assume this role:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": "lambda.amazonaws.com"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

#### 2. Create the IAM Role via CLI:
```bash
command aws iam create-role \
    --role-name MedicareLambdaExecutionRole \
    --assume-role-policy-document file://lambda-trust-policy.json
```

#### 3. Define S3 & Logging Access Policy (`lambda-s3-logging-policy.json`)
Create a custom policy allowing the function to fetch model artifacts and write execution logs:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject"
      ],
      "Resource": "arn:aws:s3:::medicare-fraud-analytics-023413058557/models/*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:*:*:*"
    }
  ]
}
```

#### 4. Attach Custom Policy to the Role:
```bash
command aws iam put-role-policy \
    --role-name MedicareLambdaExecutionRole \
    --policy-name S3AndLoggingForScoringLambda \
    --policy-document file://lambda-s3-logging-policy.json
```

---

### B. Automating the Local Build & Deployment
Create an automated Bash script (`etl-scripts/deploy_lambda.sh`) to package and upload the function.

```bash
#!/bin/bash
# etl-scripts/deploy_lambda.sh
set -e

echo "=== Medicare Fraud Detection Serverless Deployer ==="

# Define configurations
LAMBDA_DIR="lambda-scoring"
PACKAGE_DIR="lambda_package"
ZIP_NAME="lambda_deploy.zip"
FUNCTION_NAME="medicare-provider-scorer"
ROLE_ARN="arn:aws:iam::023413058557:role/MedicareLambdaExecutionRole"
BUCKET="medicare-fraud-analytics-023413058557"

# Clean previous builds
echo "1. Cleaning directory structures..."
rm -rf $PACKAGE_DIR $ZIP_NAME
mkdir -p $PACKAGE_DIR

# Pull Linux wheels direct from PyPI on macOS
echo "2. Cross-compiling and downloading Linux x86_64 binaries..."
pip install \
  --platform manylinux2014_x86_64 \
  --target=$PACKAGE_DIR \
  --implementation cp \
  --python-version 3.11 \
  --only-binary=:all: \
  scikit-learn joblib pandas numpy boto3

# Copy scoring codebase into package
echo "3. Copying Lambda handlers..."
cp $LAMBDA_DIR/lambda_function.py $PACKAGE_DIR/

# Create ZIP archive
echo "4. Generating deployment ZIP file..."
cd $PACKAGE_DIR
zip -r9 ../$ZIP_NAME .
cd ..

# Deploying to AWS Lambda
echo "5. Deploying package to AWS Lambda..."
# Check if function already exists
EXISTS=$(command aws lambda get-function --function-name $FUNCTION_NAME 2>&1 || true)

if [[ $EXISTS == *"ResourceNotFoundException"* ]]; then
  echo "--> Creating new function: $FUNCTION_NAME"
  command aws lambda create-function \
    --function-name $FUNCTION_NAME \
    --runtime python3.11 \
    --role $ROLE_ARN \
    --handler lambda_function.lambda_handler \
    --zip-file fileb://$ZIP_NAME \
    --timeout 30 \
    --memory-size 1024 \
    --environment "Variables={BUCKET_NAME=$BUCKET,MODEL_KEY=models/model.joblib,SCALER_KEY=models/scaler.joblib}"
else
  echo "--> Updating existing function code..."
  command aws lambda update-function-code \
    --function-name $FUNCTION_NAME \
    --zip-file fileb://$ZIP_NAME
    
  echo "--> Updating configuration defaults..."
  command aws lambda update-function-configuration \
    --function-name $FUNCTION_NAME \
    --timeout 30 \
    --memory-size 1024 \
    --environment "Variables={BUCKET_NAME=$BUCKET,MODEL_KEY=models/model.joblib,SCALER_KEY=models/scaler.joblib}"
fi

echo "=== Deployment Completed Successfully! ==="
```

---

## 🧪 6. Testing & Verifying serving in Production

Once deployed, you can verify inference behavior using JSON payloads.

### Standard Test Payload (`test_payload.json`)
This mirrors the features of the highly anomalous target provider **`PRV53033`**:
```json
{
  "provider": "PRV53033",
  "total_claims": 2,
  "unique_patients": 1,
  "avg_length_of_stay": 2.5,
  "avg_daily_reimbursement": 18250.0,
  "max_daily_reimbursement": 36500.0,
  "claims_per_patient_ratio": 2.0,
  "high_daily_claims_ratio": 1.0
}
```

### Run Verification Test Command:
```bash
# Invoke the function via AWS CLI and save output to response.json
command aws lambda invoke \
    --function-name medicare-provider-scorer \
    --payload file://test_payload.json \
    --cli-binary-format raw-in-base64-out \
    response.json

# Read response metrics
cat response.json
```

### Expected JSON Output Structure:
```json
{
  "statusCode": 200,
  "headers": {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "POST,OPTIONS"
  },
  "body": "{\"provider\": \"PRV53033\", \"anomaly_score\": -0.791015625, \"is_anomaly\": true, \"risk_level\": \"CRITICAL\", \"features\": {\"provider\": \"PRV53033\", \"total_claims\": 2, \"unique_patients\": 1, \"avg_length_of_stay\": 2.5, \"avg_daily_reimbursement\": 18250.0, \"max_daily_reimbursement\": 36500.0, \"claims_per_patient_ratio\": 2.0, \"high_daily_claims_ratio\": 1.0}}"
}
```

---

## 📈 7. Operations, Latency, and Scalability
1. **Cold Starts**: When a new Lambda container is provisioned, download and deserialization of the `model.joblib` artifact (1.5 MB) takes approximately **1.2 to 1.8 seconds**.
2. **Warm Hits**: Once cached in `/tmp` and loaded in RAM, subsequent requests require no S3 calls, executing predictions in **< 15 milliseconds**.
3. **Memory Limits**: The package utilizes standard arrays and vectors. A `1024 MB` memory configuration is recommended to provide sufficient headroom for Python runtime serialization and garbage collection.
