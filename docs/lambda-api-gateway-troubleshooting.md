# Serverless ML Scoring Microservice: API Gateway & Lambda Integration Troubleshooting Guide

This troubleshooting guide provides a comprehensive post-mortem of the real-time scoring microservice integration. It documents the symptoms of a critical API Gateway v2 (HTTP API) integration error, the detailed diagnostic steps used to uncover the root cause, and the exact steps taken to resolve the issue.

---

## 1. Executive Summary & Symptoms

### The Scenario
The serverless machine learning scoring pipeline serves real-time fraud risk predictions for Medicare providers (e.g., `PRV53033`). It uses:
* **API Gateway v2 (HTTP API)**: Acts as the public API entry point (`https://z2373xvbgh.execute-api.us-east-1.amazonaws.com/score`).
* **AWS Lambda (`medicare-provider-scorer`)**: A Python 3.11 microservice running an Isolation Forest anomaly detection model, caching results in **DynamoDB**, and generating generative AI evaluations with **Amazon Bedrock**.

### The Symptom
When sending a real-time prediction payload via `curl`, the client immediately received an **`HTTP/2 500 Internal Server Error`**:
```bash
curl -i -X POST https://z2373xvbgh.execute-api.us-east-1.amazonaws.com/score \
  -H "Content-Type: application/json" \
  -d @test.json

HTTP/2 500
content-type: application/json
content-length: 35
date: Tue, 15 Sep 2026 05:40:12 GMT
apigw-requestid: Duc8oiYYoAMEcNg=

{"message":"Internal Server Error"}
```

### The Diagnostic Bottleneck (No Logs!)
Upon checking CloudWatch logs to inspect the failure stack trace, we observed a critical issue:
* **Observation:** The `/aws/lambda/medicare-provider-scorer` log group contained **absolutely no new log entries or log streams** corresponding to the failed requests.
* **Why this is highly suspicious:** If a Lambda function runs and crashes due to a Python exception (e.g., SyntaxError, KeyValue error, or database timeout), AWS Lambda *always* logs the invocation start and the stack trace. The complete absence of logs indicates that **the execution was blocked before the Lambda service could even initialize the container or trigger the handler code.**

---

## 2. Diagnostics & Root Cause Discovery

To isolate the silent failure, we audited the interaction boundary between API Gateway v2 and AWS Lambda using the AWS CLI.

### Step 1: Inspect the API Gateway v2 Routing Schema
We queried the API routes and integrations to see how incoming requests to `/score` were being processed.

**Command:**
```bash
aws apigatewayv2 get-routes --api-id z2373xvbgh
```

**Discovery:**
The HTTP API was configured with a single wildcard `$default` route pointing to an AWS_PROXY integration:
* **Route Key:** `$default`
* **Integration ID:** `4iq13mc`
* **Target Integration:** `arn:aws:lambda:us-east-1:023413058557:function:medicare-provider-scorer`
* **Payload Format Version:** `2.0` (Standard for HTTP APIs)

Under this configuration, API Gateway acts as a "greedy proxy," intercepting all paths (including `/score`) and sending them directly to the Lambda function.

### Step 2: Inspect the Lambda Function's Resource-Based Policy
Since the API Gateway setup was correct, we audited the Lambda function's resource policy to verify whether API Gateway actually had permission to invoke the function.

**Command:**
```bash
aws lambda get-policy --function-name medicare-provider-scorer
```

**Discovery:**
The resource policy contained an invocation permission that was too restrictive:
```json
{
  "Version": "2012-10-17",
  "Id": "default",
  "Statement": [
    {
      "Sid": "apigateway-invoke-permission",
      "Effect": "Allow",
      "Principal": {
        "Service": "apigateway.amazonaws.com"
      },
      "Action": "lambda:InvokeFunction",
      "Resource": "arn:aws:lambda:us-east-1:023413058557:function:medicare-provider-scorer",
      "Condition": {
        "ArnLike": {
          "AWS:SourceArn": "arn:aws:execute-api:us-east-1:023413058557:z2373xvbgh/*/*/score"
        }
      }
    }
  ]
}
```

### The Root Cause
1. **The Policy Restriction:** The Lambda policy only allowed invocation if the incoming `SourceArn` matched the pattern `*/*/score`.
2. **The API Gateway Context Mismatch:** API Gateway v2 HTTP APIs routing through a `$default` route do not pass the nested explicit route contexts expected by older REST APIs or explicit sub-route configurations. Because the request was matched and routed by the `$default` fallback path on API Gateway, the IAM evaluation failed to validate the `SourceArn` against the restricted `/score` suffix.
3. **The Resulting Failure:** API Gateway's request to trigger the Lambda was rejected with an `AccessDeniedException` at the security gateway level. Because the request was blocked at AWS's front door, Lambda never spun up a container, resulting in **zero CloudWatch logs** and a generic `HTTP 500` error returned to the client.

---

## 3. Detailed Remediation Steps

To fix the integration block, we added a wildcard invocation permission to the Lambda function's resource policy, allowing the API Gateway ID (`z2373xvbgh`) to invoke the function for any path and method.

### Step 1: Add Wildcard Permission to Lambda
We executed the AWS CLI `add-permission` command to create a new permission statement (`apigateway-allow-all-routes`) allowing wildcard pathing:

```bash
aws lambda add-permission \
    --function-name medicare-provider-scorer \
    --statement-id apigateway-allow-all-routes \
    --action lambda:InvokeFunction \
    --principal apigateway.amazonaws.com \
    --source-arn "arn:aws:execute-api:us-east-1:023413058557:z2373xvbgh/*"
```

* **Why this is secure:** This still restricts invocation exclusively to our specific API Gateway instance (`z2373xvbgh`), preserving proper security isolation, while allowing the API Gateway's `$default` route context to invoke the function successfully.

### Step 2: Remove the Restrictive Permission
To clean up the security configuration and prevent future confusion, we removed the stale, restrictive permission statement:

```bash
aws lambda remove-permission \
    --function-name medicare-provider-scorer \
    --statement-id apigateway-invoke-permission
```

### Step 3: Verify the Policy is Updated
We re-queried the Lambda policy to confirm only the wildcard permission was active:

```bash
aws lambda get-policy --function-name medicare-provider-scorer
```

---

## 4. Verification and End-to-End Success

### 1. Client Curl Verification
We triggered the prediction request again with the provider payload. The API responded instantly with an **`HTTP/2 200 OK`** and the correct scoring JSON payload:

```bash
curl -i -X POST https://z2373xvbgh.execute-api.us-east-1.amazonaws.com/score \
  -H "Content-Type: application/json" \
  -d @test.json

HTTP/2 200 
content-type: application/json
content-length: 191
date: Tue, 15 Sep 2026 05:58:26 GMT
apigw-requestid: Due0DhSHoAMEcXA=

{"provider": "PRV53033", "is_anomaly": true, "anomaly_score": -0.795847, "risk_level": "CRITICAL", "cached": true, "genai_analysis": "N/A", "timestamp": "2026-09-15T05:58:26.839870"}
```

---

## 5. In-Depth CloudWatch Log Analysis

With the permission block resolved, the Lambda successfully initialized. We ran the log tracking command:

```bash
aws logs tail "/aws/lambda/medicare-provider-scorer" --since 30m
```

### The Log Stream Record
```text
2026-09-15T05:58:24.557Z INIT_START Runtime Version: python:3.11 ...
2026-09-15T05:58:24.663Z [WARNING] joblib will operate in serial mode (Permission denied for /dev/shm)
2026-09-15T05:58:26.785Z START RequestId: 2d14a76a-e726-48f7-9b8d-38b250ab62c7
2026-09-15T05:58:26.786Z [HANDLER] Real-Time Scoring Invoked at 2026-09-15T05:58:26.786180Z
2026-09-15T05:58:26.786Z [HANDLER] Event keys: ['version', 'routeKey', 'rawPath', 'rawQueryString', 'headers', 'requestContext', 'body', 'isBase64Encoded']
2026-09-15T05:58:26.786Z [HANDLER] Processing batch=False with 1 record(s).
2026-09-15T05:58:26.786Z [CACHE] Querying DynamoDB cache for provider: PRV53033 ...
2026-09-15T05:58:26.786Z [SIGV4] Preparing POST request to dynamodb.us-east-1.amazonaws.com/ ...
2026-09-15T05:58:26.786Z [SIGV4] Canonical request hash: 8dd32a54b2414bb14a13378d4427cca7a453763ed9eb6dfdcc0f5be2493e2bfb
2026-09-15T05:58:26.786Z [SIGV4] Dispatching POST request to URL: https://dynamodb.us-east-1.amazonaws.com/
2026-09-15T05:58:26.839Z [SIGV4] Success response received in 0.053s (response size: 503 bytes)
2026-09-15T05:58:26.840Z [CACHE] HIT: Found cached results for provider: PRV53033
2026-09-15T05:58:26.840Z [HANDLER] COMPLETED: Serving cache-hit results for provider PRV53033.
2026-09-15T05:58:26.842Z END RequestId: 2d14a76a-e726-48f7-9b8d-38b250ab62c7
2026-09-15T05:58:26.842Z REPORT RequestId: 2d14a76a-e726-48f7-9b8d-38b250ab62c7 Duration: 56.13 ms Billed Duration: 2281 ms Memory Size: 1024 MB Max Memory Used: 139 MB Init Duration: 2224.80 ms
```

### Breaking Down the Execution Lifecycle
1. **Cold Start Overhead (`INIT_START` & `Init Duration`):**
   * Since this was a fresh container deployment, AWS had to provision the virtual environment. 
   * The initialization took **2,224.80 ms**, which includes importing large scientific packages (`scikit-learn`, `pandas`, `numpy`).
2. **Joblib Shared Memory Warning:**
   * `UserWarning: [Errno 13] Permission denied. joblib will operate in serial mode`
   * **Why this occurs:** AWS Lambda restricts direct filesystem access to shared memory `/dev/shm` for multi-threading. Joblib (the model serializer) automatically detects this and gracefully falls back to execute in a single-threaded serial mode. **This is fully expected and is the correct behaviour under serverless constraints.**
3. **Event Verification:**
   * The handler received the API Gateway v2 event payload cleanly and extracted the raw string `body` parameter.
4. **Programmatic AWS SigV4 Handshake (`[SIGV4]`):**
   * To keep our deployment package ultra-lean (under the 250MB limit) and avoid importing the slow, heavy `boto3`/`botocore` libraries (~80MB unzipped), the microservice uses a hand-rolled, purely native Python Signer using standard library cryptographic wrappers (`hmac`, `hashlib`, `urllib.request`).
   * The signer calculated the canonical query request signature and successfully dispatched a secure raw JSON payload directly to the DynamoDB endpoint over HTTPS.
5. **Ultra-Fast Cache Lookup (`[CACHE] HIT`):**
   * The native HTTPS query to DynamoDB resolved in a blistering **53 milliseconds**!
   * It successfully fetched a cached prediction for `PRV53033`. Since the score was already computed, the handler bypassed the model scoring and Bedrock analysis pipelines, serving the cached prediction instantly to ensure low latency and save cost.
6. **Execution Efficiency:**
   * **Actual Runtime:** **56.13 ms** (excluding Cold Start init).
   * **Memory Footprint:** Used **139 MB** out of the **1024 MB** allocated, proving that the microservice is running highly efficient, cost-effective serverless code.

---

## 6. Lessons Learned & Best Practices

1. **Verify Lambda Access Policies First for Silent 500s:** If a gateway or client-facing HTTP endpoint returns `HTTP 500` but CloudWatch is completely blank, always audit resource policies and trigger security permissions first. The invocation is likely being blocked at the AWS service boundary.
2. **API Gateway v2 `$default` Wildcards:** When using the `$default` greedy routing context inside API Gateway v2, ensure your Lambda invocation permissions allow the corresponding wildcard `/*` source ARN to accommodate dynamic proxy routing contexts.
3. **Avoid heavy dependencies:** Keep Lambda zip packages small by omitting `boto3` when direct HTTP endpoints can be queried using a lightweight hand-rolled SigV4 client. This prevents cold starts from ballooning into 10+ seconds.
