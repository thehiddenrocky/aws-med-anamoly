. Save this script as test_sample_on_lambda.py

    1 import pandas as pd
    2 import json
    3 import boto3
    4
    5 def main():
    6     # 1. Read the raw sample data
    7     csv_path = 'datasets/samples/train_inpatient_sample.csv'
    8     print(f"Reading raw claims from {csv_path}...")
    9     df = pd.read_csv(csv_path)
   10
   11     # 2. Filter for a specific provider from your sample
   12     provider_id = 'PRV55912'
   13     provider_claims = df[df['Provider'] == provider_id].copy()
   14
   15     if provider_claims.empty:
   16         print(f"Provider {provider_id} not found in sample.")
   17         return
   18
   19     # 3. Simulate the PySpark ETL Aggregation
   20     # Convert dates to calculate length of stay
   21     provider_claims['ClaimStartDt'] = pd.to_datetime(provider_claims['ClaimStartDt'])
   22     provider_claims['ClaimEndDt'] = pd.to_datetime(provider_claims['ClaimEndDt'])
   23     provider_claims['length_of_stay'] = (provider_claims['ClaimEndDt'] -
      provider_claims['ClaimStartDt']).dt.days
   24
   25     # Avoid division by zero for same-day discharges
   26     provider_claims['length_of_stay'] = provider_claims['length_of_stay'].apply(lambda x: 1 if x == 0
      else x)
   27     provider_claims['daily_reimbursement'] = provider_claims['InscClaimAmtReimbursed'] /
      provider_claims['length_of_stay']
   28
   29     features = {
   30         "provider": provider_id,
   31         "total_claims": int(len(provider_claims)),
   32         "unique_patients": int(provider_claims['BeneID'].nunique()),
   33         "avg_length_of_stay": float(provider_claims['length_of_stay'].mean()),
   34         "avg_daily_reimbursement": float(provider_claims['daily_reimbursement'].mean()),
   35         "max_daily_reimbursement": float(provider_claims['daily_reimbursement'].max()),
   36         "claims_per_patient_ratio": float(len(provider_claims) / provider_claims['BeneID'].nunique()),
   37         "high_daily_claims_ratio": 0.0  # Simplified mock for the test script
   38     }
   39
   40     print(f"\n--- Extracted Features for {provider_id} ---")
   41     print(json.dumps(features, indent=2))
   42
   43     # 4. Invoke Lambda (Mocking the API Gateway Payload structure)
   44     print("\n--- Invoking AWS Lambda ---")
   45
   46     # NOTE: Ensure your AWS CLI credentials are active in your terminal
   47     lambda_client = boto3.client('lambda', region_name='us-east-1')
   48
   49     api_gateway_payload = {
   50         "body": json.dumps(features)
   51     }
   52
   53     try:
   54         response = lambda_client.invoke(
   55             FunctionName='medicare-anomaly-scoring', # <-- Update this if your Lambda name is different
   56             InvocationType='RequestResponse',
   57             Payload=json.dumps(api_gateway_payload)
   58         )
   59
   60         result = json.loads(response['Payload'].read())
   61         print("\n--- Lambda Response ---")
   62         print(json.dumps(result, indent=2))
   63
   64     except Exception as e:
   65         print(f"Failed to invoke Lambda: {e}")
   66
   67 if __name__ == "__main__":
   68     main()

  2. Run the script using your virtual environment

  Once you've saved the script, you can run it purely locally to test the cloud endpoint:

   1 venv/bin/python test_sample_on_lambda.py

  Alternative: Testing purely via CLI
  If you just want to trigger the Lambda directly using the test_payload.json file we created earlier (which
  already contains pre-aggregated features), you can run:

   1 command aws lambda invoke \
   2     --function-name medicare-anomaly-scoring \
   3     --payload fileb://test_payload.json \
   4     response.json && cat response.json
