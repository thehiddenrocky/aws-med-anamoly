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
import time

def load_aws_credentials():
    """
    Attempts to load AWS credentials from credentials/aws-access-key.json and set them as 
    environment variables. This allows local scripts and tests to run seamlessly 
    using these credentials without needing environment-level export commands.
    """
    # Look in possible paths (current directory, or directory up, or relative to script)
    possible_paths = [
        os.path.join(os.getcwd(), 'credentials/aws-access-key.json'),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'credentials/aws-access-key.json'),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'credentials/aws-access-key.json'),
        'credentials/aws-access-key.json'
    ]
    
    for path in possible_paths:
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    data = json.load(f)
                
                access_key = None
                secret_key = None
                session_token = None
                
                if isinstance(data, dict):
                    # Check nested "AccessKey" (aws iam create-access-key output)
                    if 'AccessKey' in data and isinstance(data['AccessKey'], dict):
                        ak = data['AccessKey']
                        access_key = ak.get('AccessKeyId') or ak.get('accessKeyId')
                        secret_key = ak.get('SecretAccessKey') or ak.get('secretAccessKey')
                    
                    # Fall back to top-level key lookups with different casings
                    if not access_key:
                        access_key = (
                            data.get('aws_access_key_id') or 
                            data.get('AWS_ACCESS_KEY_ID') or 
                            data.get('AccessKeyId') or 
                            data.get('access_key')
                        )
                    if not secret_key:
                        secret_key = (
                            data.get('aws_secret_access_key') or 
                            data.get('AWS_SECRET_ACCESS_KEY') or 
                            data.get('SecretAccessKey') or 
                            data.get('secret_key')
                        )
                    session_token = (
                        data.get('aws_session_token') or 
                        data.get('AWS_SESSION_TOKEN') or 
                        data.get('SessionToken') or 
                        data.get('session_token')
                    )
                
                if access_key and secret_key:
                    os.environ['AWS_ACCESS_KEY_ID'] = access_key
                    os.environ['AWS_SECRET_ACCESS_KEY'] = secret_key
                    if session_token:
                        os.environ['AWS_SESSION_TOKEN'] = session_token
                    print(f"[CREDENTIALS] Successfully loaded AWS credentials from {path}")
                    return True
            except Exception as e:
                print(f"[CREDENTIALS] Warning: Failed to parse credentials file at {path}: {e}")
    return False

# Load credentials on startup
load_aws_credentials()

def sign(key, msg):
    """
    Helper function to compute the HMAC-SHA256 hash of a message using a key.
    Required by the sequential keys derivation process of AWS Signature Version 4.
    """
    return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()

def get_signature_key(key, date_stamp, regionName, serviceName):
    """
    Derives the SigV4 Signing Key using a cryptographic chain of nested HMAC-SHA256 hashes.
    
    Instead of signing directly with your raw Secret Access Key, AWS uses a scoped
    derived key. This prevents security leaks from affecting other days, regions, or services.
    
    Derivation Process:
    1. KeyDate    = HMAC( "AWS4" + SecretKey, DateStamp )  -- binds the key to a specific day
    2. KeyRegion  = HMAC( KeyDate, Region )                -- binds the key to a specific AWS region
    3. KeyService = HMAC( KeyRegion, Service )            -- binds the key to a specific AWS service
    4. KeySigning = HMAC( KeyService, "aws4_request" )     -- completes the scope derivation
    """
    # Initialize the derivation chain with the raw secret key prefixed with 'AWS4'
    kDate = sign(('AWS4' + key).encode('utf-8'), date_stamp)
    kRegion = sign(kDate, regionName)
    kService = sign(kRegion, serviceName)
    kSigning = sign(kService, 'aws4_request')
    return kSigning

def aws_sigv4_request(service, region, host, endpoint, method, payload_str, action_header=None):
    """
    Constructs and executes a signed AWS API Request using Signature Version 4.
    Runs entirely on Python standard libraries (`urllib.request`) to eliminate Boto3/Botocore.
    """
    print(f"[SIGV4] Preparing {method} request to {service}.{region}.amazonaws.com{endpoint} ...")
    
    # 1. Retrieve raw credentials from the standard Lambda runtime environment
    access_key = os.environ.get('AWS_ACCESS_KEY_ID')
    secret_key = os.environ.get('AWS_SECRET_ACCESS_KEY')
    session_token = os.environ.get('AWS_SESSION_TOKEN')
    
    if not access_key or not secret_key:
        print("[SIGV4] ERROR: AWS credentials missing in environment variables.")
        raise ValueError("AWS credentials not found in environment.")
        
    # 2. Get current timestamp and formatted dates
    t = datetime.datetime.utcnow()
    amz_date = t.strftime('%Y%m%dT%H%M%SZ')  # ISO 8601 basic format (e.g., '20260913T120000Z')
    date_stamp = t.strftime('%Y%m%d')        # YYYYMMDD format (e.g., '20260913')
    
    canonical_uri = endpoint
    canonical_querystring = ''  # Query parameters are not used for these DynamoDB/Bedrock JSON payloads
    
    # 3. Formulate the HTTP Headers required for request signing
    headers = {
        'host': host,
        'x-amz-date': amz_date,
        'content-type': 'application/x-amz-json-1.0' if service == 'dynamodb' else 'application/json'
    }
    # Session tokens are required when running inside AWS Lambda execution environments
    if session_token:
        headers['x-amz-security-token'] = session_token
    # DynamoDB uses x-amz-target to identify API actions (e.g., GetItem, PutItem)
    if action_header:
        headers['x-amz-target'] = action_header
        
    # 4. Construct the Canonical Request
    # This standardizes the HTTP request components so that both client and server compute 
    # the exact same cryptographic hash, avoiding discrepancies in formatting.
    # Step A: Sort all header keys alphabetically
    sorted_header_keys = sorted(headers.keys())
    # Step B: Create a string of lowercase sorted header keys and their values
    canonical_headers = '\n'.join([f"{k}:{headers[k]}" for k in sorted_header_keys]) + '\n'
    # Step C: Create a list of the signed header keys, joined by semicolons
    signed_headers = ';'.join(sorted_header_keys)
    
    # Step D: Hash the request payload body with SHA256
    payload_hash = hashlib.sha256(payload_str.encode('utf-8')).hexdigest()
    
    # Step E: Combine components into the Canonical Request
    # Format: METHOD \n URI \n QUERY_STRING \n CANONICAL_HEADERS \n SIGNED_HEADERS \n PAYLOAD_HASH
    canonical_request = f"{method}\n{canonical_uri}\n{canonical_querystring}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    print(f"[SIGV4] Canonical request hash: {hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}")
    
    # 5. Construct the String to Sign
    # This meta-string binds the cryptographic signature to a specific algorithm, time, and scope.
    algorithm = 'AWS4-HMAC-SHA256'
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    # Format: ALGORITHM \n TIMESTAMP \n CREDENTIAL_SCOPE \n HASH_OF_CANONICAL_REQUEST
    string_to_sign = f"{algorithm}\n{amz_date}\n{credential_scope}\n{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    
    # 6. Generate the Signature
    # Step A: Cryptographically derive the scoped key
    signing_key = get_signature_key(secret_key, date_stamp, region, service)
    # Step B: HMAC sign the String to Sign with the derived key
    signature = hmac.new(signing_key, string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()
    
    # 7. Build the Authorization Header
    # Format: AWS4-HMAC-SHA256 Credential=<AccessKeyID>/<Scope>, SignedHeaders=<Headers>, Signature=<HexSignature>
    authorization_header = f"{algorithm} Credential={access_key}/{credential_scope}, SignedHeaders={signed_headers}, Signature={signature}"
    
    # 8. Dispatch the Request
    request_headers = headers.copy()
    request_headers['Authorization'] = authorization_header
    
    url = f"https://{host}{endpoint}"
    print(f"[SIGV4] Dispatching {method} request to URL: {url} (payload size: {len(payload_str)} bytes)")
    req = urllib.request.Request(url, data=payload_str.encode('utf-8'), headers=request_headers, method=method)
    
    try:
        start_time = time.time()
        with urllib.request.urlopen(req) as response:
            res_body = response.read().decode('utf-8')
            elapsed = time.time() - start_time
            print(f"[SIGV4] Success response received in {elapsed:.3f}s (response size: {len(res_body)} bytes)")
            return res_body
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8', errors='ignore')
        print(f"[SIGV4] ERROR: AWS HTTP Error {e.code} {e.reason}")
        print(f"[SIGV4] Error Response Body: {error_body}")
        raise

def aws_s3_download(bucket, key, local_path, region='us-east-1'):
    """
    Downloads a binary file from S3 using SigV4 without boto3.
    """
    print(f"[S3] Downloading file from S3: s3://{bucket}/{key} -> {local_path} ...")
    access_key = os.environ.get('AWS_ACCESS_KEY_ID')
    secret_key = os.environ.get('AWS_SECRET_ACCESS_KEY')
    session_token = os.environ.get('AWS_SESSION_TOKEN')
    
    if not access_key or not secret_key:
        print("[S3] ERROR: AWS credentials missing in environment variables.")
        raise ValueError("AWS credentials not found in environment.")
        
    t = datetime.datetime.utcnow()
    amz_date = t.strftime('%Y%m%dT%H%M%SZ')
    date_stamp = t.strftime('%Y%m%d')
    
    # S3 virtual host style host
    host = f"{bucket}.s3.{region}.amazonaws.com"
    endpoint = f"/{key}"
    method = 'GET'
    
    # S3 GET request has empty payload; SHA256 of empty string is a constant value
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
    
    # Ensure local directory exists
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    
    try:
        start_time = time.time()
        with urllib.request.urlopen(req) as response:
            data = response.read()
            with open(local_path, 'wb') as f:
                f.write(data)
        elapsed = time.time() - start_time
        print(f"[S3] SUCCESS: Downloaded {len(data)} bytes in {elapsed:.3f}s to {local_path}")
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8', errors='ignore')
        print(f"[S3] DOWNLOAD ERROR: HTTP {e.code} {e.reason} - S3 Response: {error_body}")
        raise

# Global state cache for Model & Scaler (survives across warm starts)
MODEL = None
SCALER = None

# S3 Configuration
BUCKET_NAME = os.environ.get('BUCKET_NAME', 'medicare-fraud-analytics-023413058557').strip()
MODEL_KEY = os.environ.get('MODEL_KEY', 'models/model.joblib').strip()
SCALER_KEY = os.environ.get('SCALER_KEY', 'models/scaler.joblib').strip()
S3_REGION = os.environ.get('S3_REGION', os.environ.get('AWS_REGION', 'us-east-1')).strip()

# DynamoDB Configuration
DYNAMODB_TABLE = os.environ.get('DYNAMODB_TABLE', 'medicare_provider_scores').strip()
DYNAMODB_REGION = os.environ.get('DYNAMODB_REGION', os.environ.get('AWS_REGION', 'us-east-1')).strip()

# Bedrock Configuration
BEDROCK_MODEL_ID = os.environ.get('BEDROCK_MODEL_ID', 'us.anthropic.claude-haiku-4-5-20251001-v1:0').strip()
BEDROCK_REGION = os.environ.get('BEDROCK_REGION', 'us-east-1').strip()

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
    region = S3_REGION
    
    print("[ARTIFACTS] Checking machine learning artifacts in memory...")
    
    # 1. Load Scaler
    if SCALER is None:
        if not os.path.exists(LOCAL_SCALER_PATH):
            print(f"[ARTIFACTS] Cache miss on disk for Scaler. Initiating S3 download...")
            aws_s3_download(BUCKET_NAME, SCALER_KEY, LOCAL_SCALER_PATH, region=region)
        else:
            print("[ARTIFACTS] Scaler binary found on local disk cache (/tmp).")
            
        print("[ARTIFACTS] Deserializing Scaler into memory using joblib...")
        start_time = time.time()
        SCALER = joblib.load(LOCAL_SCALER_PATH)
        print(f"[ARTIFACTS] Scaler loaded successfully in {time.time() - start_time:.3f}s")
    else:
        print("[ARTIFACTS] Scaler already warm-cached in Lambda container memory.")
        
    # 2. Load Isolation Forest Model
    if MODEL is None:
        if not os.path.exists(LOCAL_MODEL_PATH):
            print(f"[ARTIFACTS] Cache miss on disk for Model. Initiating S3 download...")
            aws_s3_download(BUCKET_NAME, MODEL_KEY, LOCAL_MODEL_PATH, region=region)
        else:
            print("[ARTIFACTS] Isolation Forest model binary found on local disk cache (/tmp).")
            
        print("[ARTIFACTS] Deserializing Isolation Forest Model into memory using joblib...")
        start_time = time.time()
        MODEL = joblib.load(LOCAL_MODEL_PATH)
        print(f"[ARTIFACTS] Model loaded successfully in {time.time() - start_time:.3f}s")
    else:
        print("[ARTIFACTS] Isolation Forest model already warm-cached in Lambda container memory.")

def check_dynamodb_cache(provider_id):
    """
    Checks if an analysis already exists in the DynamoDB cache.
    Returns the parsed body dict on hit, or None on miss.
    """
    if not DYNAMODB_TABLE:
        print("[CACHE] DynamoDB table name not configured. Skipping cache lookup.")
        return None
        
    region = DYNAMODB_REGION
    host = f"dynamodb.{region}.amazonaws.com"
    endpoint = "/"
    action = "DynamoDB_20120810.GetItem"
    
    payload = {
        "TableName": DYNAMODB_TABLE,
        "Key": {
            "provider": {"S": provider_id}
        }
    }
    
    print(f"[CACHE] Querying DynamoDB cache for provider: {provider_id} ...")
    try:
        response_str = aws_sigv4_request(
            service="dynamodb",
            region=region,
            host=host,
            endpoint=endpoint,
            method="POST",
            payload_str=json.dumps(payload),
            action_header=action
        )
        response_data = json.loads(response_str)
        item = response_data.get("Item")
        if item:
            print(f"[CACHE] HIT: Found cached results for provider: {provider_id}")
            # Map DynamoDB AttributeValues to standard Python types
            return {
                "provider": item["provider"]["S"],
                "anomaly_score": float(item["anomaly_score"]["N"]),
                "is_anomaly": item["is_anomaly"]["BOOL"],
                "risk_level": item["risk_level"]["S"],
                "genai_analysis": item["genai_analysis"]["S"],
                "features": json.loads(item["features_json"]["S"])
            }
        else:
            print(f"[CACHE] MISS: No cache entry found for provider: {provider_id}")
            return None
    except Exception as e:
        print(f"[CACHE] Warning: Error querying DynamoDB cache: {str(e)}. Proceeding to model inference.")
        return None

def invoke_bedrock_analysis(provider_id, score, risk_level, metrics):
    """
    Invokes AWS Bedrock (Claude 4.5 Haiku) to generate a concise summary of flagged anomalies.
    """
    if not BEDROCK_MODEL_ID:
        print("[GENAI] Bedrock Model ID not configured. Skipping GenAI analysis.")
        return "N/A"
        
    region = BEDROCK_REGION
    host = f"bedrock-runtime.{region}.amazonaws.com"
    endpoint = f"/model/{BEDROCK_MODEL_ID}/invoke"
    
    # Construct a highly focused, specific prompt
    prompt = (
        f"Analyze these provider features: provider={provider_id}, total_claims={metrics.get('total_claims')}, "
        f"unique_patients={metrics.get('unique_patients')}, avg_length_of_stay={metrics.get('avg_length_of_stay', 0.0):.2f}, "
        f"avg_daily_reimbursement={metrics.get('avg_daily_reimbursement', 0.0):.2f}, max_daily_reimbursement={metrics.get('max_daily_reimbursement', 0.0):.2f}, "
        f"claims_per_patient_ratio={metrics.get('claims_per_patient_ratio', 0.0):.2f}, high_daily_claims_ratio={metrics.get('high_daily_claims_ratio', 0.0):.2f}.\n"
        f"The provider has an anomaly score of {score:.6f} with a risk level of {risk_level}.\n"
        f"Identify which metrics look highly suspicious and suggest what a fraud investigator should examine."
    )
    
    payload = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 150,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": prompt
                    }
                ]
            }
        ],
        "system": "You are an expert Medicare fraud investigation assistant. Analyze the provided clinical/financial billing metrics of the provider against the anomaly detection output. Generate a professional, objective 2-sentence summary detailing exactly which features look suspicious (e.g., high claims-to-patient ratio or excessive length of stay) and what an investigator should look into."
    }
    
    print(f"[GENAI] Invoking Bedrock ({BEDROCK_MODEL_ID}) for provider: {provider_id} ...")
    try:
        response_str = aws_sigv4_request(
            service="bedrock-runtime",
            region=region,
            host=host,
            endpoint=endpoint,
            method="POST",
            payload_str=json.dumps(payload)
        )
        response_data = json.loads(response_str)
        content = response_data.get("content", [])
        if content and len(content) > 0:
            summary = content[0].get("text", "").strip()
            print(f"[GENAI] SUCCESS: Bedrock analysis generated for {provider_id}.")
            return summary
        else:
            print("[GENAI] Warning: Empty text response received from Bedrock.")
            return "N/A"
    except Exception as e:
        print(f"[GENAI] Error invoking Bedrock: {str(e)}. Falling back to 'N/A' summary.")
        return "N/A"

def save_to_dynamodb_cache(provider_id, score, is_anomaly, risk_level, genai_analysis, features):
    """
    Saves a completed analysis back to the DynamoDB cache.
    """
    if not DYNAMODB_TABLE:
        return
        
    region = DYNAMODB_REGION
    host = f"dynamodb.{region}.amazonaws.com"
    endpoint = "/"
    action = "DynamoDB_20120810.PutItem"
    
    # Calculate TTL (expire after 30 days)
    ttl_epoch = int((datetime.datetime.utcnow() + datetime.timedelta(days=30)).timestamp())
    timestamp_iso = datetime.datetime.utcnow().isoformat() + "Z"
    
    payload = {
        "TableName": DYNAMODB_TABLE,
        "Item": {
            "provider": {"S": provider_id},
            "anomaly_score": {"N": f"{score:.6f}"},
            "is_anomaly": {"BOOL": is_anomaly},
            "risk_level": {"S": risk_level},
            "genai_analysis": {"S": genai_analysis},
            "features_json": {"S": json.dumps(features)},
            "timestamp": {"S": timestamp_iso},
            "ttl": {"N": str(ttl_epoch)}
        }
    }
    
    print(f"[CACHE] Saving results to DynamoDB for provider: {provider_id} ...")
    try:
        aws_sigv4_request(
            service="dynamodb",
            region=region,
            host=host,
            endpoint=endpoint,
            method="POST",
            payload_str=json.dumps(payload),
            action_header=action
        )
        print(f"[CACHE] SUCCESS: Saved provider {provider_id} to cache.")
    except Exception as e:
        print(f"[CACHE] Error saving to DynamoDB cache: {str(e)}")

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
    Processes incoming provider scoring requests, orchestrating S3 artifact loaded models,
    DynamoDB caching, and Bedrock (Claude 4.5 Haiku) summary enrichment.
    """
    print("=" * 80)
    print(f"[HANDLER] Real-Time Scoring Invoked at {datetime.datetime.utcnow().isoformat()}Z")
    print(f"[HANDLER] Event keys: {list(event.keys())}")
    
    # Handle preflight CORS request (triggered by API Gateway)
    # Detects both REST API ('httpMethod') and HTTP API v2 ('requestContext' -> 'http' -> 'method')
    http_method = event.get('httpMethod')
    if not http_method and 'requestContext' in event:
        http_method = event['requestContext'].get('http', {}).get('method')
        
    if http_method == 'OPTIONS':
        print("[HANDLER] Preflight OPTIONS check. Returning success with CORS headers.")
        return format_response(200, {"message": "Success"})
        
    # 1. Parse Event Body if called via API Gateway Proxy / HTTP API
    if 'body' in event:
        body_str = event.get('body')
        if not body_str:
            return format_response(400, {"error": "Empty body in request."})
        try:
            data = json.loads(body_str)
        except Exception as e:
            print(f"[HANDLER] ERROR: Failed to parse JSON body: {str(e)}")
            return format_response(400, {"error": f"Invalid JSON payload: {str(e)}"})
    else:
        # Fall back to event as raw JSON if invoked directly (e.g. CLI direct invoke)
        data = event

    # 2. Check for Batch or Single Request
    is_batch = isinstance(data, list)
    records = data if is_batch else [data]
    print(f"[HANDLER] Processing batch={is_batch} with {len(records)} record(s).")
    
    # 3. If Single Record, perform a quick DynamoDB Cache lookup to bypass ML and GenAI latency
    if not is_batch and len(records) == 1:
        provider_id = records[0].get('provider')
        if provider_id:
            cached_result = check_dynamodb_cache(provider_id)
            if cached_result:
                print(f"[HANDLER] COMPLETED: Serving cache-hit results for provider {provider_id}.")
                print("=" * 80)
                return format_response(200, cached_result)

    # 4. Validate Inputs and Align Features
    processed_inputs = []
    providers = []
    
    print("[HANDLER] Validating input structures and extracting features...")
    for i, record in enumerate(records):
        provider_id = record.get('provider', f'UNKNOWN_{i}')
        providers.append(provider_id)
        
        # Ensure all required ML numerical inputs are provided
        missing_fields = [f for f in REQUIRED_FEATURES if f not in record]
        if missing_fields:
            print(f"[HANDLER] ERROR: Missing features for {provider_id}: {missing_fields}")
            return format_response(400, {
                "error": f"Missing features in payload for provider {provider_id}: {missing_fields}"
            })
            
        # Extract features in exact mathematical order
        try:
            features = [float(record[f]) for f in REQUIRED_FEATURES]
            processed_inputs.append(features)
            print(f"[HANDLER] Record {i+1}/{len(records)} ({provider_id}) validated.")
        except (ValueError, TypeError) as e:
            print(f"[HANDLER] ERROR: Invalid data type for features of provider {provider_id}: {str(e)}")
            return format_response(400, {
                "error": f"Invalid data type for features of provider {provider_id}: {str(e)}"
            })

    try:
        # 5. Lazy Load Models (runs on Cold Starts only)
        load_artifacts()
    except Exception as e:
        print(f"[HANDLER] CRITICAL: FAILED TO LOAD MODELS: {str(e)}")
        return format_response(500, {"error": f"Internal model loading failure: {str(e)}"})

    try:
        # 6. Format input as DataFrame to match training pipelines
        print("[HANDLER] Building DataFrame for Scikit-Learn input...")
        input_df = pd.DataFrame(processed_inputs, columns=REQUIRED_FEATURES)
        
        # 7. Normalize Features using cached Scaler
        print("[HANDLER] Normalizing features using loaded Scaler...")
        X_scaled = SCALER.transform(input_df)
        
        # 8. Generate Anomaly Scores (more negative -> higher risk/outlier likelihood)
        print("[HANDLER] Scoring inputs using Isolation Forest...")
        inference_start = time.time()
        anomaly_scores = MODEL.score_samples(X_scaled)
        
        # 9. Predict Outlier Flag (-1 = Anomaly, 1 = Normal)
        predictions = MODEL.predict(X_scaled)
        print(f"[HANDLER] ML Inference complete in {time.time() - inference_start:.4f}s.")
        
        # 10. Structure Response Results & enrich high-risk cases with Bedrock GenAI analyses
        results = []
        for idx, provider_id in enumerate(providers):
            score = float(anomaly_scores[idx])
            flag = int(predictions[idx])
            record = records[idx]
            
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
                
            print(f"[HANDLER] Provider: {provider_id} -> Anomaly Score: {score:.6f}, Risk: {risk_level}, Outlier Flag: {flag}")
            
            # Bedrock analysis is only triggered for HIGH or CRITICAL anomalies to conserve token budget
            genai_analysis = "N/A"
            if risk_level in ["HIGH", "CRITICAL"]:
                print(f"[HANDLER] High risk level detected. Generating GenAI investigation summary...")
                genai_analysis = invoke_bedrock_analysis(provider_id, score, risk_level, record)
            else:
                print(f"[HANDLER] Risk level is {risk_level}. Skipping GenAI Bedrock summary.")
                
            # If single request, write results to DynamoDB cache
            if not is_batch:
                save_to_dynamodb_cache(provider_id, score, flag == -1, risk_level, genai_analysis, record)
                
            results.append({
                "provider": provider_id,
                "anomaly_score": score,
                "is_anomaly": True if flag == -1 else False,
                "risk_level": risk_level,
                "genai_analysis": genai_analysis,
                "features": record # echoing original features back
            })
            
        print("[HANDLER] SUCCESS: Returning final inference response.")
        print("=" * 80)
        return format_response(200, results if is_batch else results[0])

    except Exception as e:
        print(f"[HANDLER] CRITICAL PREDICTION FAILURE: {str(e)}")
        print("=" * 80)
        return format_response(500, {"error": f"Inference pipeline execution error: {str(e)}"})

