# Proposed Integration Plan: Real-Time GenAI Fraud Analysis & Caching (Boto3-Free Optimization)

This document details the optimized implementation plan to integrate AWS Bedrock (Claude 4.5 Haiku) and Amazon DynamoDB caching directly into the real-time scoring Lambda (`medicare-provider-scorer`) **without using `boto3` or `botocore`**. 

This plan is strictly aligned with the API Gateway proxy integration format verified in `test_test_sample_on_lambda.py`.

---

## 🗺️ High-Level Flow (Request-Response Lifecycle)

```text
                  [ User API Gateway Proxy Request ] (POST /score)
                  Payload format: { "body": "{\"provider\": \"PRV57070\", ...}" }
                                         │
                                         ▼
                     [ Parse Input Event & Extract Provider NPI ]
                                         │
                                         ▼
                  [ Check DynamoDB Cache via GetItem (SigV4) ]
                                         │
             ┌───────────────────────────┴───────────────────────────┐
             ▼ (Cache Hit)                                           ▼ (Cache Miss)
     [ Standard API response ]                               [ Run Isolation Forest ML ]
     - Status: 200                                           - Calculate score (-0.81)
     - Headers: CORS-enabled                                 - Determine Risk Level
     - Body: Cached fields + GenAI analysis                            │
                                                                       ▼
                                                             [ Is Risk Level ≥ HIGH? ]
                                                                       │
                                                       ┌───────────────┴───────────────┐
                                                       ▼ (Yes)                         ▼ (No)
                                               [ Amazon Bedrock ]                [ Skip Bedrock ]
                                               - Claude 4.5 Haiku                - Set analysis to "N/A"
                                               - Direct Invoke (SigV4)
                                                       │                               │
                                                       └───────────────┬───────────────┘
                                                                       ▼
                                                          [ Save to DynamoDB Cache ]
                                                          - Write via PutItem (SigV4)
                                                                       │
                                                                       ▼
                                                           [ Standard API Response ]
                                                           - Return status 200
                                                           - Body with ML score & GenAI analysis
```

---

## 📐 Prompt Engineering Strategy (for Claude 4.5 Haiku)

When a provider is flagged as **HIGH** or **CRITICAL** risk, we invoke Bedrock with a structured prompt.

**System Instructions:**
```text
You are an expert Medicare fraud investigation assistant. Analyze the provided clinical/financial billing metrics of the provider against the anomaly detection output. Generate a professional, objective 2-sentence summary detailing exactly which features look suspicious (e.g., high claims-to-patient ratio or excessive length of stay) and what an investigator should look into.
```

**Dynamic Prompt Payload Structure:**
```json
{
  "provider": "PRV57070",
  "anomaly_score": -0.815200,
  "risk_level": "CRITICAL",
  "metrics": {
    "total_claims": 845,
    "unique_patients": 12,
    "avg_length_of_stay": 14.2,
    "avg_daily_reimbursement": 3450.0,
    "max_daily_reimbursement": 12000.0,
    "claims_per_patient_ratio": 70.4,
    "high_daily_claims_ratio": 0.05
  }
}
```

---

## 🗄️ DynamoDB Schema Design (`medicare_provider_scores`)

The DynamoDB table keeps the schema aligned with the structured JSON body returned by the API proxy format.

* **Partition Key (PK)**: `provider` (String, e.g., `"PRV57070"`)
* **Attributes**:
    * `anomaly_score` (Number, e.g., `-0.815200`)
    * `is_anomaly` (Boolean, e.g., `true`)
    * `risk_level` (String, e.g., `"CRITICAL"`)
    * `genai_analysis` (String, human-readable summary or `"N/A"`)
    * `features_json` (String, JSON-serialized string of the input metrics)
    * `timestamp` (String, ISO-8601 creation timestamp, e.g., `"2026-09-13T12:00:00Z"`)
    * `ttl` (Number, UNIX Epoch timestamp to auto-expire the cache after 30 days)

---

## ⚙️ Boto3-Free AWS SigV4 Request Signer (Standard Library Only)

Standard AWS Lambda Python runtimes pre-inject standard credentials (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`) into environment variables. The SigV4 signer handles request formatting, signing, and execution natively.

### Core SigV4 Helper Implementation Details

```python
import os
import hmac
import hashlib
import datetime
import urllib.request
import urllib.error
import json

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
```

---

## 🛠️ Direct AWS Service Payloads

### 1. DynamoDB: `GetItem` (Cache Lookup)
* **Endpoint**: `/`
* **X-Amz-Target**: `DynamoDB_20120810.GetItem`
* **JSON Payload**:
```json
{
  "TableName": "medicare_provider_scores",
  "Key": {
    "provider": { "S": "PRV57070" }
  }
}
```

### 2. DynamoDB: `PutItem` (Cache Save)
* **Endpoint**: `/`
* **X-Amz-Target**: `DynamoDB_20120810.PutItem`
* **JSON Payload**:
```json
{
  "TableName": "medicare_provider_scores",
  "Item": {
    "provider": { "S": "PRV57070" },
    "anomaly_score": { "N": "-0.815200" },
    "is_anomaly": { "BOOL": true },
    "risk_level": { "S": "CRITICAL" },
    "genai_analysis": { "S": "Dr. PRV57070 is flagged at CRITICAL risk..." },
    "features_json": { "S": "{\"total_claims\": 845, ...}" },
    "timestamp": { "S": "2026-09-13T12:00:00Z" },
    "ttl": { "N": "1789300800" }
  }
}
```

### 3. AWS Bedrock: `InvokeModel` (Claude 4.5 Haiku via US Inference Profile)
* **Endpoint**: `/model/us.anthropic.claude-haiku-4-5-20251001-v1:0/invoke`
* **Host**: `bedrock-runtime.us-east-1.amazonaws.com`
* **JSON Payload**:
```json
{
  "anthropic_version": "bedrock-2023-05-31",
  "max_tokens": 500,
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "Analyze these provider features: total_claims=845, unique_patients=12, claims_per_patient_ratio=70.4. Explain why this constitutes a high anomaly score."
        }
      ]
    }
  ]
}
```

---

## 📋 Implementation Steps for Lambda Integration

1. **Integrate Helpers in `lambda-scoring.py`**: Inject the standard library SigV4 helper and request handlers into the main Lambda file.
2. **Implement Sequential Integration Logic**:
   * **Step A (Parse)**: Extract features from the nested event body:
     ```python
     body_str = event.get('body', '{}') or '{}'
     data = json.loads(body_str)
     provider_id = data.get('provider')
     ```
   * **Step B (Cache Lookup)**: Call DynamoDB `GetItem` via pure Python SigV4. If found, deserialize attributes and return the response immediately wrapped inside standard `format_response(200, cached_body)`:
     ```python
     # Example deserializer logic inside lambda
     cached_body = {
         "provider": item["provider"]["S"],
         "anomaly_score": float(item["anomaly_score"]["N"]),
         "is_anomaly": item["is_anomaly"]["BOOL"],
         "risk_level": item["risk_level"]["S"],
         "genai_analysis": item["genai_analysis"]["S"],
         "features": json.loads(item["features_json"]["S"])
     }
     return format_response(200, cached_body)
     ```
   * **Step C (ML Inference)**: On cache miss, run scikit-learn models as usual to calculate score, outlier flag, and risk level.
   * **Step D (GenAI Enriched Summary)**: If the risk level is `HIGH` or `CRITICAL`, construct the Bedrock dynamic prompt payload and trigger `aws_sigv4_request` for Bedrock to get the explanation.
   * **Step E (Cache Write)**: Save results (ML scores, features, and GenAI summaries) back to DynamoDB via `PutItem` so future requests hitting Lambda bypass both execution and model-inference latency entirely.
   * **Step F (Respond)**: Format CORS CORS-enabled HTTP output exactly matching proxy specifications.
