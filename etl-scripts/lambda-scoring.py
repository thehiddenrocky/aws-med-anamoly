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
