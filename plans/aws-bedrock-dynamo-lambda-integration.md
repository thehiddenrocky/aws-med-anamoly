Proposed Integration Plan: Real-Time GenAI Fraud Analysis & Caching

  Now that we have verified that the Bedrock inference profile
  us.anthropic.claude-haiku-4-5-20251001-v1:0 is fully authorized and functional, we can
  integrate it into our real-time scoring Lambda (medicare-provider-scorer).

  cmd ran to validate 

  aws bedrock-runtime invoke-model --model-id "us.anthropic.claude-haiku-4-5-20251001-v1:0" --body '{"anthropic_version": "bedrock-2023-05-31", "max_tokens": 500, "messages": [{"role": "user", "content": [{"type": "text", "text": "Explain in one sentence why a provider anomaly score of -0.8 represents a high risk in fraud detection."}]}]}' --cli-binary-format raw-in-base64-out --region us-east-1 response.json

  1. High-Level Flow (Request-Response Lifecycle)

    1                   [ User API Request ] (POST /score { "provider": "12345", ... })
    2                             │
    3                             ▼
    4                [ Check DynamoDB Cache ]
    5                             │
    6              ┌──────────────┴──────────────┐
    7              ▼ (Cache Hit)                 ▼ (Cache Miss / Forced Refresh)
    8      [ Return Saved Response ]     [ Run local Isolation Forest ML model ]
    9      - Score & Risk Level          - Calculate anomaly score (-0.82)
   10      - Bedrock Analysis Summary    - Categorize risk (CRITICAL/HIGH/MEDIUM/LOW)
   11                                            │
   12                                            ▼
   13                                  [ Is Risk Level ≥ HIGH? ]
   14                                            │
   15                              ┌─────────────┴─────────────┐
   16                              ▼ (Yes)                     ▼ (No)
   17                      [ Amazon Bedrock ]            [ Skip Bedrock ]
   18                      - Model: Claude 4.5 Haiku     - Set analysis to "N/A"
   19                      - Generate brief summary
   20                              │                           │
   21                              └─────────────┬─────────────┘
   22                                            ▼
   23                                 [ Save to DynamoDB Cache ]
   24                                            │
   25                                            ▼
   26                                [ CORS-Enabled JSON Output ]

  ---

  2. Prompt Engineering Strategy (for Claude 4.5 Haiku)

  When a provider is flagged as HIGH or CRITICAL risk, we will invoke Bedrock with a structured
  prompt.

  System Instructions:
  > You are an expert Medicare fraud investigation assistant. Analyze the provided
  clinical/financial billing metrics of the provider against the anomaly detection output.
  Generate a professional, objective 2-sentence summary detailing exactly which features look
  suspicious (e.g., high claims-to-patient ratio or excessive length of stay) and what an
  investigator should look into.

  Dynamic Prompt Payload:

    1 {
    2   "provider": "1234567890",
    3   "anomaly_score": -0.8152,
    4   "risk_level": "CRITICAL",
    5   "metrics": {
    6     "total_claims": 845,
    7     "unique_patients": 12,
    8     "avg_length_of_stay": 14.2,
    9     "avg_daily_reimbursement": 3450.0,
   10     "claims_per_patient_ratio": 70.4
   11   }
   12 }

  Targeted Output:
  > "Dr. 1234567890 is flagged at CRITICAL risk due to an extreme claims-per-patient ratio of
  70.4, indicating possible code-upcoding or billing for services not rendered. Additionally,
  an average length of stay of 14.2 days is highly anomalous for typical outpatient clinical
  baselines, warranting immediate medical record audits."

  ---

  3. DynamoDB Schema Design (medicare_provider_scores)

  To prevent duplicate Bedrock calls and keep latency at sub-10ms for subsequent requests, we
  will cache the scores and analysis in DynamoDB.

   * Partition Key (PK): provider (String, e.g., "1234567890")
   * Attributes:
       * anomaly_score (Number, e.g., -0.8152)
       * is_anomaly (Boolean, e.g., true)
       * risk_level (String, e.g., "CRITICAL")
       * genai_analysis (String, human-readable summary or "N/A")
       * features (Map/JSON of the input features)
       * timestamp (String, ISO-8601 creation timestamp)
       * ttl (Number, UNIX Epoch timestamp to auto-expire the cache after 30 days)

  ---

  4. Implementation Steps in Lambda (etl-scripts/lambda-scoring.py)

   1. Initialize AWS Clients: Add boto3.client('bedrock-runtime') and
      boto3.resource('dynamodb') outside the handler.
   2. Add Cache Lookup: Before processing, query the DynamoDB table. If found, return cached
      values immediately.
   3. Perform ML Inference: Run Isolation Forest prediction as normal.
   4. Invoke Bedrock (Conditional): If the provider is scored as HIGH or CRITICAL, call
      invoke_model using the inference profile ID us.anthropic.claude-haiku-4-5-20251001-v1:0.
   5. Write Cache: Save the unified result back to DynamoDB with a 30-day Time to Live (TTL).
   6. Update IAM Policies: Update the lambda execution role policy
      (lamdba-s3-logging-policy.json) to allow:
       * bedrock:InvokeModel on
         arn:aws:bedrock:us-east-1::foundation-model/us.anthropic.claude-haiku-4-5-20251001-v1:
         0
       * dynamodb:GetItem, dynamodb:PutItem on the target DynamoDB table.

  ---

  Ready to proceed?
  If this plan looks solid, please let me know and I will begin updating the Lambda code,
  policy documents, and local testing scripts.
▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄
 > what is dynamic db here
▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
  Responding with gemini-3.5-flash
 Thinking...
 │
 │ Clarifying DynamoDB Query
 │ I'm now zeroing in on the user's DynamoDB query. It seems they need clarification, possibly
 │ on terminology ("dynamic db") or its role in the context of my earlier explanation. I am
 │ making sure they are getting the intended meaning.

✦ "Dynamic DB" refers to Amazon DynamoDB, AWS's fully managed NoSQL database.

  In our architecture, DynamoDB acts as a fast caching and storage layer between the ML/GenAI
  logic and the frontend. Here is exactly why we use it:

  1. Ultra-Low Latency (Sub-millisecond)
   * Invoking an ML model and calling the Bedrock LLM to generate a summary can take 1 to 3
     seconds.
   * If a user (like a fraud investigator) clicks on a provider profile, we cannot make them
     wait 3 seconds every single time.
   * By saving the results to DynamoDB, the API can fetch the provider's score and AI summary
     in less than 10 milliseconds on subsequent requests.

  2. Cost Control (Saves Money)
   * Every call to Bedrock (Claude 4.5 Haiku) costs money per token.
   * If multiple investigators are looking at the same flagged provider, or if the UI requests
     the same provider profile repeatedly, we read the summary from DynamoDB instead of
     invoking Bedrock again. This reduces your AWS bill significantly.

  3. Simple NoSQL Structure
  Since every provider has a unique National Provider Identifier (NPI) as their ID, we use a
  simple key-value structure:
   * Key: Provider ID (NPI)
   * Value: { Anomaly Score, Risk Level, Bedrock AI Summary, Timestamp }

  ---
