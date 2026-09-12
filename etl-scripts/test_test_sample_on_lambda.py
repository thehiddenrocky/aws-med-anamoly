import pandas as pd
import json
import subprocess
import os
import sys

def score_provider(df, provider_id):
    """
    Helper function to aggregate features for a single provider and invoke Lambda.
    Returns the parsed results from Lambda, or None on error.
    """
    provider_claims = df[df['Provider'] == provider_id].copy()
    if provider_claims.empty:
        return None

    # Calculate length of stay
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

    api_gateway_payload = {
        "body": json.dumps(features)
    }

    temp_payload_path = f'temp_payload_{provider_id}.json'
    temp_response_path = f'temp_response_{provider_id}.json'

    result = None
    try:
        # Write payload to temp file
        with open(temp_payload_path, 'w') as f:
            json.dump(api_gateway_payload, f)

        # Invoke using 'command aws'
        cmd = f"command aws lambda invoke --function-name medicare-provider-scorer --payload file://{temp_payload_path} --cli-binary-format raw-in-base64-out {temp_response_path}"
        process = subprocess.run(cmd, shell=True, capture_output=True, text=True)

        # AWS CLI v1 fallback
        if process.returncode != 0:
            stderr_lower = process.stderr.lower()
            if "unknown option" in stderr_lower or "cli-binary-format" in stderr_lower:
                cmd_fallback = f"command aws lambda invoke --function-name medicare-provider-scorer --payload file://{temp_payload_path} {temp_response_path}"
                process = subprocess.run(cmd_fallback, shell=True, capture_output=True, text=True)

        if process.returncode == 0 and os.path.exists(temp_response_path):
            with open(temp_response_path, 'r') as f:
                lambda_response = json.load(f)
            
            if "body" in lambda_response:
                body_data = json.loads(lambda_response["body"])
                result = {
                    "provider": provider_id,
                    "total_claims": features["total_claims"],
                    "unique_patients": features["unique_patients"],
                    "anomaly_score": body_data.get("anomaly_score"),
                    "is_anomaly": body_data.get("is_anomaly"),
                    "risk_level": body_data.get("risk_level")
                }
    except Exception as e:
        pass
    finally:
        # Clean up temp files
        for path in [temp_payload_path, temp_response_path]:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass
    return result

def main():
    csv_path = 'datasets/samples/test_inpatient_sample.csv'
    raw_test_source_path = 'datasets/kaggle-med-fraud-detection/Test_Inpatientdata-1542969243754.csv'

    # Ensure the test sample file exists
    if not os.path.exists(csv_path):
        print(f"Test sample file '{csv_path}' not found.")
        if os.path.exists(raw_test_source_path):
            print(f"Creating test sample from raw source: '{raw_test_source_path}'...")
            try:
                os.makedirs(os.path.dirname(csv_path), exist_ok=True)
                df_raw = pd.read_csv(raw_test_source_path)
                df_sample = df_raw.head(1000)
                df_sample.to_csv(csv_path, index=False)
                print(f"Successfully generated '{csv_path}' with 1000 claims.")
            except Exception as e:
                print(f"Failed to generate test sample from raw source: {e}")
                return
        else:
            print(f"Error: Neither '{csv_path}' nor raw source '{raw_test_source_path}' could be found.")
            return

    # 1. Read the raw sample data
    print(f"Reading raw test claims from {csv_path}...")
    df = pd.read_csv(csv_path)

    # 2. Get top 10 providers list
    available_providers_counts = df['Provider'].dropna().value_counts()
    top_providers = available_providers_counts.head(10)

    loop_all = False
    provider_id = None

    if len(sys.argv) > 1:
        arg = sys.argv[1].strip()
        if arg in ('--list', '-l', '--help', '-h'):
            print("\n--- Top Providers in Test Sample Dataset ---")
            for idx, (p_id, count) in enumerate(top_providers.items(), start=1):
                print(f"{idx:2d}. {p_id:<12} ({count} claim(s))")
            print("\nTo score a specific provider, run:")
            print("  python etl-scripts/test_test_sample_on_lambda.py <PROVIDER_ID>")
            print("\nTo automatically score all top 10 providers in a loop, run:")
            print("  python etl-scripts/test_test_sample_on_lambda.py --loop")
            return
        elif arg in ('--loop', '--all', '-loop', '-all', '-a'):
            loop_all = True
            print("\nAuto-loop mode active: scoring the top 10 providers...")
        else:
            provider_id = arg
            print(f"Targeting specified provider: '{provider_id}'")
    else:
        provider_id = 'PRV57070'
        print(f"No Provider ID specified. Defaulting to '{provider_id}'.")
        print("\nNote: You can run this script to query other providers:")
        print("  python etl-scripts/test_test_sample_on_lambda.py <PROVIDER_ID>")
        print("\nOr list top available providers in the dataset:")
        print("  python etl-scripts/test_test_sample_on_lambda.py --list")
        print("\nOr score all top 10 providers automatically in a loop:")
        print("  python etl-scripts/test_test_sample_on_lambda.py --loop")

    # If looping over all providers:
    if loop_all:
        results = []
        print("\nScoring providers...")
        for p_id in top_providers.index:
            print(f" - Processing and scoring {p_id}...", end="", flush=True)
            res = score_provider(df, p_id)
            if res:
                results.append(res)
                print(" Done.")
            else:
                print(" Failed.")

        # Print beautiful consolidated summary report
        print("\n" + "="*80)
        print(f"{'CONSOLIDATED FRAUD SCORING REPORT':^80}")
        print("="*80)
        print(f"{'Provider ID':<12} | {'Claims':<8} | {'Patients':<8} | {'Anomaly Score':<15} | {'Risk Level':<12} | {'Is Anomaly':<10}")
        print("-"*80)
        for r in results:
            score_str = f"{r['anomaly_score']:.6f}" if r['anomaly_score'] is not None else "N/A"
            risk_str = r['risk_level'] if r['risk_level'] else "N/A"
            is_anom_str = str(r['is_anomaly']) if r['is_anomaly'] is not None else "N/A"
            print(f"{r['provider']:<12} | {r['total_claims']:<8d} | {r['unique_patients']:<8d} | {score_str:<15} | {risk_str:<12} | {is_anom_str:<10}")
        print("="*80)
        return

    # Single-provider mode:
    provider_claims = df[df['Provider'] == provider_id].copy()

    if provider_claims.empty:
        print(f"\nError: Provider '{provider_id}' not found in the test sample dataset.")
        print("\nHere are the top available providers you can query:")
        for idx, (p_id, count) in enumerate(top_providers.items(), start=1):
            print(f"  - {p_id:<12} ({count} claim(s))")
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

        # Invoke using 'command aws'
        cmd = f"command aws lambda invoke --function-name medicare-provider-scorer --payload file://{temp_payload_path} --cli-binary-format raw-in-base64-out {temp_response_path}"
        
        print(f"Executing: {cmd}")
        process = subprocess.run(cmd, shell=True, capture_output=True, text=True)

        # AWS CLI v1 fallback
        if process.returncode != 0:
            stderr_lower = process.stderr.lower()
            if "unknown option" in stderr_lower or "cli-binary-format" in stderr_lower:
                print("AWS CLI v1 detected (unrecognized --cli-binary-format option). Retrying without it...")
                cmd_fallback = f"command aws lambda invoke --function-name medicare-provider-scorer --payload file://{temp_payload_path} {temp_response_path}"
                process = subprocess.run(cmd_fallback, shell=True, capture_output=True, text=True)
            else:
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
