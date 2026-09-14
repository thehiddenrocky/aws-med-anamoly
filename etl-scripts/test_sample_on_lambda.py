import pandas as pd
import json
import subprocess
import os

def load_aws_credentials():
    """
    Attempts to load AWS credentials from credentials/aws-access-key.json and set them as 
    environment variables so the AWS CLI subprocess inherits them.
    """
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
                    if 'AccessKey' in data and isinstance(data['AccessKey'], dict):
                        ak = data['AccessKey']
                        access_key = ak.get('AccessKeyId') or ak.get('accessKeyId')
                        secret_key = ak.get('SecretAccessKey') or ak.get('secretAccessKey')
                    
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

def main():
    # Load credentials first so child CLI processes inherit them
    load_aws_credentials()

    # 1. Read the raw sample data
    csv_path = 'datasets/samples/train_inpatient_sample.csv'
    print(f"Reading raw claims from {csv_path}...")
    df = pd.read_csv(csv_path)

    # 2. Filter for a specific provider from your sample
    provider_id = 'PRV55912'
    provider_claims = df[df['Provider'] == provider_id].copy()

    if provider_claims.empty:
        print(f"Provider {provider_id} not found in sample.")
        return

    # 3. Simulate the PySpark ETL Aggregation
    # Convert dates to calculate length of stay
    provider_claims['ClaimStartDt'] = pd.to_datetime(provider_claims['ClaimStartDt'])
    provider_claims['ClaimEndDt'] = pd.to_datetime(provider_claims['ClaimEndDt'])
    provider_claims['length_of_stay'] = (provider_claims['ClaimEndDt'] - provider_claims['ClaimStartDt']).dt.days

    # Avoid division by zero for same-day discharges
    provider_claims['length_of_stay'] = provider_claims['length_of_stay'].apply(lambda x: 1 if x == 0 else x)
    provider_claims['daily_reimbursement'] = provider_claims['InscClaimAmtReimbursed'] / provider_claims['length_of_stay']

    features = {
        "provider": provider_id,
        "total_claims": int(len(provider_claims)),
        "unique_patients": int(provider_claims['BeneID'].nunique()),
        "avg_length_of_stay": float(provider_claims['length_of_stay'].mean()),
        "avg_daily_reimbursement": float(provider_claims['daily_reimbursement'].mean()),
        "max_daily_reimbursement": float(provider_claims['daily_reimbursement'].max()),
        "claims_per_patient_ratio": float(len(provider_claims) / provider_claims['BeneID'].nunique()),
        "high_daily_claims_ratio": 0.0  # Simplified mock for the test script
    }

    print(f"\n--- Extracted Features for {provider_id} ---")
    print(json.dumps(features, indent=2))

    # 4. Invoke Lambda (Mocking the API Gateway Payload structure)
    print("\n--- Invoking AWS Lambda ---")

    api_gateway_payload = {
        "body": json.dumps(features)
    }

    temp_payload_path = 'temp_payload.json'
    temp_response_path = 'temp_response.json'

    try:
        # Write payload to temp file
        with open(temp_payload_path, 'w') as f:
            json.dump(api_gateway_payload, f)

        # Invoke using 'command aws' to bypass shell alias interference.
        # AWS CLI v2 requires --cli-binary-format raw-in-base64-out for passing JSON payloads.
        cmd = f"command aws lambda invoke --function-name medicare-provider-scorer --payload file://{temp_payload_path} --cli-binary-format raw-in-base64-out {temp_response_path}"
        
        print(f"Executing: {cmd}")
        process = subprocess.run(cmd, shell=True, capture_output=True, text=True)

        # If it failed, check why
        if process.returncode != 0:
            stderr_lower = process.stderr.lower()
            # If the failure is because --cli-binary-format is unknown (meaning it's AWS CLI v1)
            if "unknown option" in stderr_lower or "cli-binary-format" in stderr_lower:
                print("AWS CLI v1 detected (unrecognized --cli-binary-format option). Retrying without it...")
                cmd_fallback = f"command aws lambda invoke --function-name medicare-provider-scorer --payload file://{temp_payload_path} {temp_response_path}"
                process = subprocess.run(cmd_fallback, shell=True, capture_output=True, text=True)
            else:
                # This is a real execution error (e.g., credentials, function not found, etc.)
                print(f"\nFailed to invoke Lambda via AWS CLI.\nSTDOUT: {process.stdout}\nSTDERR: {process.stderr}")
                return

        if process.returncode == 0:
            if os.path.exists(temp_response_path):
                with open(temp_response_path, 'r') as f:
                    result = json.load(f)
                print("\n--- Lambda Response ---")
                print(json.dumps(result, indent=2))
            else:
                print("Lambda execution completed, but response file not found.")
        else:
            print(f"Failed to invoke Lambda via AWS CLI after retry.\nSTDOUT: {process.stdout}\nSTDERR: {process.stderr}")

    except Exception as e:
        print(f"An error occurred during invocation: {e}")
    finally:
        # Cleanup temporary files
        for path in [temp_payload_path, temp_response_path]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass

if __name__ == "__main__":
    main()
