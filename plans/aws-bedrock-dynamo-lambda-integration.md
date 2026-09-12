# Proposed Integration Plan: Real-Time GenAI Fraud Analysis & Caching (Boto3-Free Optimization)

This document details the optimized implementation plan to integrate AWS Bedrock (Claude 4.5 Haiku) and Amazon DynamoDB caching directly into the real-time scoring Lambda (`medicare-provider-scorer`) **without using `boto3` or `botocore`**. 

By using standard Python libraries (`urllib.request`, `hashlib`, `hmac`, `datetime`) to construct and sign raw AWS API requests using Signature Version 4 (SigV4), we achieve:
* **Zero External Dependencies**: Smaller zip deployment packages.
* **Extreme Memory Optimization**: Avoids the ~100MB+ memory import overhead of `boto3`/`botocore`.
* **Ultra-Fast Cold Starts**: Execution starts in milliseconds since no heavy SDK packages are loaded.

---

## 🗺️ High-Level Flow (Request-Response Lifecycle)

```text
                  [ User API Request ] (POST /score { "provider": "12345", ... })
                            │
                            ▼
               [ Check DynamoDB Cache ]  ◄─── Direct HTTP call via urllib (GetItem + SigV4)
                            │
             ┌──────────────┴──────────────┐
             ▼ (Cache Hit)                 ▼ (Cache Miss / Forced Refresh)
     [ Return Saved Response ]     [ Run local Isolation Forest ML model ]
     - Score & Risk Level          - Calculate anomaly score (-0.82)
     - Bedrock Analysis Summary    - Categorize risk (CRITICAL/HIGH/MEDIUM/LOW)
                                           │
                                           ▼
                                 [ Is Risk Level ≥ HIGH? ]
                                           │
                             ┌─────────────┴─────────────┐
                             ▼ (Yes)                     ▼ (No)
                     [ Amazon Bedrock ]            [ Skip Bedrock ]
                     - Model: Claude 4.5 Haiku     - Set analysis to "N/A"
                     - Direct HTTP POST (SigV4)    
                             │                           │
                             └─────────────┬─────────────┘
                                           ▼
                                [ Save to DynamoDB Cache ] ◄── Direct HTTP call via urllib (PutItem + SigV4)
                                           │
                                           ▼
                               [ CORS-Enabled JSON Output ]
```

---

## 📐 Prompt Engineering Strategy (for Claude 4.5 Haiku)

When a provider is flagged as **HIGH** or **CRITICAL** risk, we invoke Bedrock with a structured prompt.

**System Instructions:**
> You are an expert Medicare fraud investigation assistant. Analyze the provided clinical/financial billing metrics of the provider against the anomaly detection output. Generate a professional, objective 2-sentence summary detailing exactly which features look suspicious (e.g., high claims-to-patient ratio or excessive length of stay) and what an investigator should look into.

**Dynamic Prompt Payload:**
```json
{
  "provider": "1234567890",
  "anomaly_score": -0.8152,
  "risk_level": "CRITICAL",
  "metrics": {
    "total_claims": 845,
    "unique_patients": 12,
    "avg_length_of_stay": 14.2,
    "avg_daily_reimbursement": 3450.0,
    "claims_per_patient_ratio": 70.4
  }
}
```

---

## 🗄️ DynamoDB Schema Design (`medicare_provider_scores`)

* **Partition Key (PK)**: `provider` (String, e.g., `"1234567890"`)
* **Attributes**:
    * `anomaly_score` (Number, e.g., `-0.8152`)
    * `is_anomaly` (Boolean, e.g., `true`)
    * `risk_level` (String, e.g., `"CRITICAL"`)
    * `genai_analysis` (String, human-readable summary or `"N/A"`)
    * `features` (Map/JSON of the input features)
    * `timestamp` (String, ISO-8601 creation timestamp)
    * `ttl` (Number, UNIX Epoch timestamp to auto-expire the cache after 30 days)

---

## ⚙️ Boto3-Free AWS SigV4 Request Signer (Standard Library Only)

To make requests to DynamoDB and Bedrock without `boto3`, we implement a pure Python helper module inside the Lambda handler. This module reads AWS IAM credentials from the Lambda environment (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`) and signs HTTP requests.

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
    
    # 1. Headers required for signing
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
    
    with urllib.request.urlopen(req) as response:
        return response.read().decode('utf-8')
```

---

## 🛠️ Direct AWS Service Payloads

### 1. DynamoDB: `GetItem` (Cache Lookup)
* **Endpoint**: `/` (DynamoDB API uses root-level POST calls)
* **X-Amz-Target**: `DynamoDB_20120810.GetItem`
* **JSON Payload**:
```json
{
  "TableName": "medicare_provider_scores",
  "Key": {
    "provider": { "S": "1234567890" }
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
    "provider": { "S": "1234567890" },
    "anomaly_score": { "N": "-0.8152" },
    "is_anomaly": { "BOOL": true },
    "risk_level": { "S": "CRITICAL" },
    "genai_analysis": { "S": "Investigation summary text here..." },
    "features_json": { "S": "{\"total_claims\": 845, ...}" },
    "timestamp": { "S": "2026-09-13T12:00:00Z" },
    "ttl": { "N": "1789300800" }
  }
}
```

### 3. AWS Bedrock: `InvokeModel` (Claude 4.5 Haiku)
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

## 📋 Implementation Steps for Lambda Deployment

1. **Integrate Helpers in `lambda-scoring.py`**: Inject the standard library SigV4 helper and request handlers into the main Lambda file.
2. **Implement Sequential Integration Logic**:
   * **Step A**: Parse request. Extract the `provider` NPI.
   * **Step B**: Invoke DynamoDB `GetItem` via helper. If an item exists, return it immediately as a fast cache hit.
   * **Step C**: If cache miss, calculate score using local scikit-learn/joblib models loaded from `/tmp`.
   * **Step D**: If risk is `HIGH`/`CRITICAL`, invoke Bedrock (`us.anthropic.claude-haiku-4-5-20251001-v1:0`) via helper.
   * **Step E**: Save results to DynamoDB via `PutItem` helper.
   * **Step F**: Respond to client.
3. **Verify Local Mock & Testing**: Run offline simulation tests with mocked HTTP responses to ensure correctness of math models and request serialization prior to staging.
