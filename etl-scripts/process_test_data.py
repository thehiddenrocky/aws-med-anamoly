import os
import sys
import json
import boto3
import pandas as pd
import numpy as np
import joblib
from botocore.exceptions import ClientError

def engineer_features(inpatient_csv_path):
    """
    Ingests raw inpatient claims CSV, cleans columns, parses dates,
    and aggregates provider-level forensic billing features.
    """
    print(f"Reading raw inpatient claims from: {inpatient_csv_path}")
    
    # Read CSV, parsing Na/NA values properly
    df = pd.read_csv(inpatient_csv_path, na_values=['NA', 'NA ', 'nan', ''])
    print(f"Loaded {len(df)} raw claim records.")
    
    # Pre-clean date columns (handle format and missing dates)
    df['AdmissionDt'] = pd.to_datetime(df['AdmissionDt'], errors='coerce')
    df['DischargeDt'] = pd.to_datetime(df['DischargeDt'], errors='coerce')
    
    # Compute length of stay in days (DischargeDt - AdmissionDt)
    df['length_of_stay'] = (df['DischargeDt'] - df['AdmissionDt']).dt.days
    df['length_of_stay'] = df['length_of_stay'].fillna(0).astype(float)
    
    # Clean InscClaimAmtReimbursed
    df['InscClaimAmtReimbursed'] = pd.to_numeric(df['InscClaimAmtReimbursed'], errors='coerce').fillna(0.0)
    
    # daily_reimbursement_rate = Reimbursement / (length_of_stay + 1.0)
    df['daily_reimbursement_rate'] = df['InscClaimAmtReimbursed'] / (df['length_of_stay'] + 1.0)
    
    # Clean DeductibleAmtPaid
    df['deductible_paid'] = pd.to_numeric(df['DeductibleAmtPaid'], errors='coerce').fillna(0.0)
    
    # High-cost claims indicator (rate > $10,000)
    df['is_high_daily'] = (df['daily_reimbursement_rate'] > 10000.0).astype(int)
    
    print("Aggregating claims to provider level...")
    # Group by Provider to build the 7 numerical ML features
    provider_groups = df.groupby('Provider')
    
    provider_df = provider_groups.agg(
        total_claims=('ClaimID', 'nunique'),
        unique_patients=('BeneID', 'nunique'),
        avg_length_of_stay=('length_of_stay', 'mean'),
        avg_daily_reimbursement=('daily_reimbursement_rate', 'mean'),
        max_daily_reimbursement=('daily_reimbursement_rate', 'max'),
        high_daily_claims_count=('is_high_daily', 'sum')
    ).reset_index()
    
    # Calculate ratios
    provider_df['claims_per_patient_ratio'] = (provider_df['total_claims'] / provider_df['unique_patients']).round(4)
    provider_df['high_daily_claims_ratio'] = (provider_df['high_daily_claims_count'] / provider_df['total_claims']).round(4)
    
    # Clean and round metrics
    provider_df['avg_daily_reimbursement'] = provider_df['avg_daily_reimbursement'].round(2)
    provider_df['max_daily_reimbursement'] = provider_df['max_daily_reimbursement'].round(2)
    provider_df['avg_length_of_stay'] = provider_df['avg_length_of_stay'].round(2)
    
    # Rename Provider to provider
    provider_df = provider_df.rename(columns={'Provider': 'provider'})
    
    # Required features
    required_features = [
        'provider',
        'total_claims', 
        'unique_patients', 
        'avg_length_of_stay', 
        'avg_daily_reimbursement', 
        'max_daily_reimbursement', 
        'claims_per_patient_ratio', 
        'high_daily_claims_ratio'
    ]
    
    final_df = provider_df[required_features].copy()
    print(f"Aggregation complete. Engineered features for {len(final_df)} unique providers.")
    return final_df

def score_locally(features_df, model_path, scaler_path):
    """
    Loads local pre-trained model and scaler to perform offline inference.
    """
    print(f"\n--- Loading Local ML Models ({model_path}, {scaler_path}) ---")
    if not os.path.exists(model_path) or not os.path.exists(scaler_path):
        print("Error: Local model or scaler files are missing. Cannot perform local scoring.")
        return None
        
    model = joblib.load(model_path)
    scaler = joblib.load(scaler_path)
    
    ml_features = [
        'total_claims', 
        'unique_patients', 
        'avg_length_of_stay', 
        'avg_daily_reimbursement', 
        'max_daily_reimbursement', 
        'claims_per_patient_ratio', 
        'high_daily_claims_ratio'
    ]
    
    X = features_df[ml_features].fillna(0.0)
    X_scaled = scaler.transform(X)
    
    # Generate continuous anomaly scores (more negative -> higher risk)
    features_df['anomaly_score'] = model.score_samples(X_scaled)
    
    # Outlier flag: predict yields -1 (outlier) or 1 (inlier)
    predictions = model.predict(X_scaled)
    features_df['is_anomaly'] = np.where(predictions == -1, True, False)
    
    # Assign Risk Levels based on scoring heuristics
    def get_risk_level(score):
        if score < -0.70:
            return "CRITICAL"
        elif score < -0.65:
            return "HIGH"
        elif score < -0.55:
            return "MEDIUM"
        else:
            return "LOW"
            
    features_df['risk_level'] = features_df['anomaly_score'].apply(get_risk_level)
    
    # Sort by anomaly score (ascending = most anomalous first)
    features_df = features_df.sort_values(by='anomaly_score')
    return features_df

def invoke_lambda_scorer(provider_record, function_name="medicare-provider-scorer"):
    """
    Demonstrates calling the deployed AWS Lambda function using boto3.
    """
    # Initialize AWS Lambda Client
    # Uses default aws credentials on your system
    try:
        lambda_client = boto3.client('lambda', region_name='us-east-1')
        
        # Structure payload in the same format API Gateway passes to Lambda (CORS/Proxy format)
        payload = {
            "body": json.dumps(provider_record)
        }
        
        response = lambda_client.invoke(
            FunctionName=function_name,
            InvocationType='RequestResponse',
            Payload=json.dumps(payload)
        )
        
        # Parse result
        payload_response = json.loads(response['Payload'].read().decode('utf-8'))
        
        # API Gateway proxy responses contain 'statusCode' and 'body'
        if 'body' in payload_response:
            return json.loads(payload_response['body'])
        return payload_response
    except ClientError as e:
        return {"error": f"Boto3 ClientError: {str(e)}"}
    except Exception as e:
        return {"error": f"General Exception: {str(e)}"}

def main():
    print("=" * 60)
    print("Medicare Fraud Test Dataset Processing Utility")
    print("=" * 60)
    
    # Option to select test claims or sample claims
    sample_file = "datasets/samples/train_inpatient_sample.csv"
    test_file = "datasets/kaggle-med-fraud-detection/Test_Inpatientdata-1542969243754.csv"
    
    target_file = sample_file
    if os.path.exists(test_file):
        print(f"\nDiscovered actual test file at: {test_file}")
        ans = input("Do you want to run against the ACTUAL TEST file? (y/n) [default: n]: ").strip().lower()
        if ans == 'y':
            target_file = test_file
            
    print(f"\nProcessing target file: {target_file}")
    
    # 1. Feature Engineering Aggregations
    try:
        features_df = engineer_features(target_file)
    except Exception as e:
        print(f"Error during feature engineering: {str(e)}")
        return
        
    # 2. Local Machine Learning Scoring
    scored_df = score_locally(features_df, "model.joblib", "scaler.joblib")
    
    if scored_df is not None:
        print("\n" + "="*50)
        print("LOCAL SCORING INTEGRITY ASSESSMENT")
        print("="*50)
        print(f"Total scored providers: {len(scored_df)}")
        anomalies = scored_df[scored_df['is_anomaly'] == True]
        print(f"Flagged Anomalies: {len(anomalies)} ({len(anomalies)/len(scored_df)*100:.2f}%)")
        
        print("\nTop 5 Most Suspicious Providers on local scoring:")
        headers = ["Provider", "Claims", "Patients", "Avg Daily Reimb.", "Score", "Risk Level"]
        print(f"{headers[0]:<15} | {headers[1]:<10} | {headers[2]:<10} | {headers[3]:<18} | {headers[4]:<10} | {headers[5]:<10}")
        print("-" * 85)
        for _, row in scored_df.head(5).iterrows():
            print(
                f"{row['provider']:<15} | "
                f"{int(row['total_claims']):<10} | "
                f"{int(row['unique_patients']):<10} | "
                f"${row['avg_daily_reimbursement']:<17.2f} | "
                f"{row['anomaly_score']:<10.4f} | "
                f"{row['risk_level']:<10}"
            )
            
        # Save results to a CSV file for analytical inspection
        output_csv = "test_scored_results.csv"
        scored_df.to_csv(output_csv, index=False)
        print(f"\nSaved all scored results to: {output_csv}")
        
    # 3. AWS Lambda scoring demo
    print("\n" + "="*50)
    print("HOW TO RUN SCORING VIA DEPLOYED AWS LAMBDA")
    print("="*50)
    
    # Get a sample provider record to show how to structure payload
    if len(features_df) > 0:
        sample_provider = features_df.iloc[0].to_dict()
        print("\nWe can score any provider by invoking our AWS Lambda.")
        print("Here is how a single provider payload should be structured:")
        print(json.dumps(sample_provider, indent=2))
        
        # Save sample payload for manual testing with CLI
        payload_for_cli = {"body": json.dumps(sample_provider)}
        with open("lambda_test_payload.json", "w") as f:
            json.dump(payload_for_cli, f, indent=2)
        print("\n--> Saved payload to 'lambda_test_payload.json' for AWS CLI testing.")
        
        print("\nRun this command in your terminal to invoke AWS Lambda directly:")
        print("  aws lambda invoke \\")
        print("    --function-name medicare-provider-scorer \\")
        print("    --cli-binary-format raw-in-base64-out \\")
        print("    --payload file://lambda_test_payload.json \\")
        print("    lambda_output.json")
        print("  cat lambda_output.json")
        
        # Offer real-time execution via Boto3 if requested
        run_boto = input("\nDo you want to attempt live Lambda invocation via Boto3 now? (y/n) [default: n]: ").strip().lower()
        if run_boto == 'y':
            print(f"Attempting live invocation for provider '{sample_provider['provider']}'...")
            res = invoke_lambda_scorer(sample_provider)
            print("\nResponse from AWS Lambda:")
            print(json.dumps(res, indent=2))

if __name__ == "__main__":
    main()
