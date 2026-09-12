import os
import json
import joblib
import numpy as np
import pandas as pd
import hmac
import hashlib
import datetime
import urllib.request
import urllib.error

def sign(key, msg):
    return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()

def get_signature_key(key, date_stamp, regionName, serviceName):
    kDate = sign(('AWS4' + key).encode('utf-8'), date_stamp)
    kRegion = sign(kDate, regionName)
    kService = sign(kRegion, serviceName)
    kSigning = sign(kService, 'aws4_request')
    return kSigning

def aws_sigv4_request(service, region, host, endpoint, method, payload_str, action_header=None):
    """
    Constructs and executes a signed AWS API Request using Signature Version 4.
    """
    access_key = os.environ.get('AWS_ACCESS_KEY_ID')
    secret_key = os.environ.get('AWS_SECRET_ACCESS_KEY')
    session_token = os.environ.get('AWS_SESSION_TOKEN')
    
    if not access_key or not secret_key:
        raise ValueError("AWS credentials not found in environment.")
        
    t = datetime.datetime.utcnow()
    amz_date = t.strftime('%Y%m%dT%H%M%SZ')
    date_stamp = t.strftime('%Y%m%d')
    
    canonical_uri = endpoint
    canonical_querystring = ''
    
    # Headers required for signing
    headers = {
        'host': host,
        'x-amz-date': amz_date,
        'content-type': 'application/x-amz-json-1.0' if service == 'dynamodb' else 'application/json'
    }
    if session_token:
        headers['x-amz-security-token'] = session_token
    if action_header:
        headers['x-amz-target'] = action_header
        
    # Sort headers to build canonical headers
    sorted_header_keys = sorted(headers.keys())
    canonical_headers = '\n'.join([f"{k}:{headers[k]}" for k in sorted_header_keys]) + '\n'
    signed_headers = ';'.join(sorted_header_keys)
    
    # Hash the payload
    payload_hash = hashlib.sha256(payload_str.encode('utf-8')).hexdigest()
    
    # Build Canonical Request
    canonical_request = f"{method}\n{canonical_uri}\n{canonical_querystring}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    
    # Build String to Sign
    algorithm = 'AWS4-HMAC-SHA256'
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = f"{algorithm}\n{amz_date}\n{credential_scope}\n{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    
    # Generate Signature
    signing_key = get_signature_key(secret_key, date_stamp, region, service)
    signature = hmac.new(signing_key, string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()
    
    # Build Authorization Header
    authorization_header = f"{algorithm} Credential={access_key}/{credential_scope}, SignedHeaders={signed_headers}, Signature={signature}"
    
    # Add auth headers back to final request headers
    request_headers = headers.copy()
    request_headers['Authorization'] = authorization_header
    
    # Send Request
    url = f"https://{host}{endpoint}"
    req = urllib.request.Request(url, data=payload_str.encode('utf-8'), headers=request_headers, method=method)
    
    try:
        with urllib.request.urlopen(req) as response:
            return response.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8')
        print(f"AWS HTTPError: {e.code} {e.reason} - Body: {error_body}")
        raise

def aws_s3_download(bucket, key, local_path, region='us-east-1'):
    """
    Downloads a binary file from S3 using SigV4 without boto3.
    """
    access_key = os.environ.get('AWS_ACCESS_KEY_ID')
    secret_key = os.environ.get('AWS_SECRET_ACCESS_KEY')
    session_token = os.environ.get('AWS_SESSION_TOKEN')
    
    if not access_key or not secret_key:
        raise ValueError("AWS credentials not found in environment.")
        
    t = datetime.datetime.utcnow()
    amz_date = t.strftime('%Y%m%dT%H%M%SZ')
    date_stamp = t.strftime('%Y%m%d')
    
    # S3 virtual host style
    host = f"{bucket}.s3.{region}.amazonaws.com"
    endpoint = f"/{key}"
    method = 'GET'
    
    # S3 empty payload hash
    payload_hash = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
    
    headers = {
        'host': host,
        'x-amz-date': amz_date,
        'x-amz-content-sha256': payload_hash
    }
    if session_token:
        headers['x-amz-security-token'] = session_token
        
    sorted_header_keys = sorted(headers.keys())
    canonical_headers = '\n'.join([f"{k}:{headers[k]}" for k in sorted_header_keys]) + '\n'
    signed_headers = ';'.join(sorted_header_keys)
    
    canonical_request = f"{method}\n{endpoint}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    
    algorithm = 'AWS4-HMAC-SHA256'
    credential_scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = f"{algorithm}\n{amz_date}\n{credential_scope}\n{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    
    signing_key = get_signature_key(secret_key, date_stamp, region, 's3')
    signature = hmac.new(signing_key, string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()
    
    authorization_header = f"{algorithm} Credential={access_key}/{credential_scope}, SignedHeaders={signed_headers}, Signature={signature}"
    
    request_headers = headers.copy()
    request_headers['Authorization'] = authorization_header
    
    url = f"https://{host}{endpoint}"
    req = urllib.request.Request(url, headers=request_headers, method=method)
    
    # Ensure destination directory exists
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    
    try:
        with urllib.request.urlopen(req) as response:
            with open(local_path, 'wb') as f:
                f.write(response.read())
        print(f"Successfully downloaded s3://{bucket}/{key} to {local_path}")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8', errors='ignore')
        print(f"S3 Download HTTPError: {e.code} {e.reason} - Body: {error_body}")
        raise

# Global state cache for Model & Scaler (survives across warm starts)
MODEL = None
SCALER = None

# S3 Configuration
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
    
    # Get configuration region
    region = os.environ.get('AWS_REGION', 'us-east-1')
    
    # 1. Load Scaler
    if SCALER is None:
        if not os.path.exists(LOCAL_SCALER_PATH):
            print(f"Downloading scaler from s3://{BUCKET_NAME}/{SCALER_KEY} ...")
            aws_s3_download(BUCKET_NAME, SCALER_KEY, LOCAL_SCALER_PATH, region=region)
        print("Loading scaler into memory...")
        SCALER = joblib.load(LOCAL_SCALER_PATH)
        
    # 2. Load Isolation Forest Model
    if MODEL is None:
        if not os.path.exists(LOCAL_MODEL_PATH):
            print(f"Downloading model from s3://{BUCKET_NAME}/{MODEL_KEY} ...")
            aws_s3_download(BUCKET_NAME, MODEL_KEY, LOCAL_MODEL_PATH, region=region)
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
