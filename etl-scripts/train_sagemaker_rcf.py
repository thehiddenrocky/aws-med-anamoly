import os
import sys
import argparse
import pandas as pd
import numpy as np
import boto3
import sagemaker
from sagemaker import RandomCutForest

def get_sagemaker_role():
    """Tries to find a suitable IAM role for SageMaker."""
    iam = boto3.client('iam')
    try:
        # Search for standard execution roles in the account
        roles = iam.list_roles(MaxItems=100)
        for role in roles['Roles']:
            role_name = role['RoleName']
            if 'SageMaker' in role_name or 'sagemaker' in role_name.lower() or 'Glue' in role_name:
                print(f"Auto-detected execution role candidate: {role['Arn']}")
                return role['Arn']
    except Exception as e:
        print(f"Warning: Could not list IAM roles: {e}")
    
    # Standard placeholder role name to return
    return "arn:aws:iam::023413058557:role/MedicareGlueETLRole"

def main():
    parser = argparse.ArgumentParser(description="Orchestrate SageMaker Built-in Random Cut Forest (RCF)")
    parser.add_argument("--role-arn", type=str, help="IAM Role ARN for SageMaker")
    parser.add_argument("--bucket", type=str, default="medicare-fraud-analytics-023413058557", help="S3 bucket for training data and artifacts")
    parser.add_argument("--use-spot", action="store_true", help="Use Managed Spot Instances for training to fit within active quotas")
    parser.add_argument("--local", action="store_true", help="Run in SageMaker Local Mode using Docker on your machine")
    args = parser.parse_args()

    print("=" * 60)
    print("SageMaker Built-in Random Cut Forest Orchestrator")
    print("=" * 60)

    # 1. Paths and inputs verification
    train_csv = "rcf_train.csv"
    mapping_csv = "provider_mapping.csv"

    if not os.path.exists(train_csv) or not os.path.exists(mapping_csv):
        print("Error: Preparation files ('rcf_train.csv' or 'provider_mapping.csv') are missing.")
        print("Please run: python etl-scripts/prepare_rcf_data.py first!")
        sys.exit(1)

    # Initialize SageMaker session
    if args.local:
        print("Initializing SageMaker Local Session (using local Docker environment)...")
        from sagemaker.local import LocalSession
        sagemaker_session = LocalSession()
        region = sagemaker_session.boto_region_name or "us-east-1"
    else:
        sagemaker_session = sagemaker.Session()
        region = sagemaker_session.boto_region_name
    print(f"AWS Region: {region}")

    # Determine execution role
    role_arn = args.role_arn if args.role_arn else get_sagemaker_role()
    print(f"Using IAM Role ARN: {role_arn}")

    bucket_name = args.bucket
    print(f"Using S3 Bucket: {bucket_name}")

    # 2. Upload training data to S3
    s3_train_prefix = "rcf/train"
    print(f"\nUploading local '{train_csv}' to s3://{bucket_name}/{s3_train_prefix}/ ...")
    
    s3_client = boto3.client('s3')
    s3_client.upload_file(train_csv, bucket_name, f"{s3_train_prefix}/{train_csv}")
    s3_train_uri = f"s3://{bucket_name}/{s3_train_prefix}/{train_csv}"
    print(f"Training data successfully available at: {s3_train_uri}")

    # Determine instance type based on local mode
    instance_type = "local" if args.local else "ml.m5.large"

    # 3. Create and Train the SageMaker RCF Estimator
    print(f"\nInitializing SageMaker Random Cut Forest (RCF) Estimator on {instance_type}...")
    
    extra_estimator_args = {}
    if args.use_spot and not args.local:
        print("Enabling Managed Spot Training for the RCF Estimator...")
        extra_estimator_args["use_spot_instances"] = True
        extra_estimator_args["max_run"] = 3600
        extra_estimator_args["max_wait"] = 7200

    rcf_estimator = RandomCutForest(
        role=role_arn,
        instance_count=1,
        instance_type=instance_type,
        num_trees=200,                  # Matches Isolation Forest trees count
        num_samples_per_tree=256,       # Default partition sample size
        output_path=f"s3://{bucket_name}/rcf/output/",
        sagemaker_session=sagemaker_session,
        **extra_estimator_args
    )

    print(f"Fitting Random Cut Forest on SageMaker (this initiates a training job on {instance_type})...")
    rcf_estimator.fit(rcf_estimator.record_set(pd.read_csv(train_csv, header=None).values))

    # 4. Perform Batch Transform to compute anomaly scores
    print(f"\nSetting up Batch Transform job for scoring on {instance_type}...")
    transformer = rcf_estimator.transformer(
        instance_count=1,
        instance_type=instance_type,
        output_path=f"s3://{bucket_name}/rcf/predictions/",
        accept="text/csv"               # Flat list of scores as CSV
    )

    print("Executing Batch Transform (running inference over all profiles)...")
    transformer.transform(
        data=s3_train_uri,
        content_type="text/csv",
        split_type="Line"
    )
    print("Waiting for Batch Transform job to complete...")
    transformer.wait()

    # 5. Download and process the predictions
    print("\nBatch Transform job complete! Downloading scores from S3...")
    predictions_prefix = f"rcf/predictions/{train_csv}.out"
    local_scores_path = "rcf_scores.csv"

    try:
        s3_client.download_file(bucket_name, predictions_prefix, local_scores_path)
        print(f"Downloaded scores to '{local_scores_path}'.")
    except Exception as e:
        # Sometimes SageMaker appends .out to the input file name, check for standard transformer output file patterns
        print(f"Retrying downloading under fallback output filename...")
        try:
            # List outputs in predictions folder
            response = s3_client.list_objects_v2(Bucket=bucket_name, Prefix="rcf/predictions/")
            for obj in response.get('Contents', []):
                if obj['Key'].endswith('.out') or obj['Key'].endswith('.csv.out'):
                    predictions_prefix = obj['Key']
                    break
            s3_client.download_file(bucket_name, predictions_prefix, local_scores_path)
            print(f"Downloaded scores from '{predictions_prefix}' to '{local_scores_path}'.")
        except Exception as ex:
            print(f"Failed to download predictions: {ex}")
            sys.exit(1)

    # 6. Interpret scores and match back to providers
    print("\nAnalyzing scores and identifying anomalies...")
    providers_df = pd.read_csv(mapping_csv)
    scores = pd.read_csv(local_scores_path, header=None, names=["anomaly_score"])

    if len(providers_df) != len(scores):
        print(f"Warning: Row mismatch! Providers: {len(providers_df)}, Scores: {len(scores)}")
        # Truncate or pad to align if needed, but they should match
        min_len = min(len(providers_df), len(scores))
        providers_df = providers_df.iloc[:min_len]
        scores = scores.iloc[:min_len]

    df_results = pd.concat([providers_df, scores], axis=1)

    # Random Cut Forest outputs positive real scores (higher means more anomalous)
    # Define threshold for top 5% (contamination rate 0.05)
    threshold = np.percentile(df_results["anomaly_score"], 95)
    df_results["is_anomaly"] = df_results["anomaly_score"] >= threshold

    anomalous_count = df_results["is_anomaly"].sum()
    print(f"\nAnalysis Complete!")
    print(f"Total Providers Analyzed: {len(df_results)}")
    print(f"RCF Anomaly Score Threshold (95th percentile): {threshold:.4f}")
    print(f"Flagged Anomalous Providers: {anomalous_count} ({anomalous_count / len(df_results) * 100:.2f}%)")

    # 7. Print top anomalous providers
    print("\nTop 10 Most Anomalous Providers (Sorted by SageMaker RCF Score):")
    top_anomalies = df_results.sort_values(by="anomaly_score", ascending=False).head(10)
    
    print("-" * 50)
    print(f"{'Provider ID':<20} | {'SageMaker RCF Score':<20} | {'Status':<10}")
    print("-" * 50)
    for _, row in top_anomalies.iterrows():
        print(f"{row['provider']:<20} | {row['anomaly_score']:<20.4f} | {'ANOMALY'}")
    print("-" * 50)

    # 8. Check specific known forensic anomalies (to compare against local Isolation Forest)
    known_targets = ['PRV53033', 'PRV52627', 'PRV53471']
    print(f"\nAudit of Known Forensic Targets:")
    audit_df = df_results[df_results['provider'].isin(known_targets)]
    if not audit_df.empty:
        for _, row in audit_df.iterrows():
            status = "ANOMALY (FLAGGED)" if row['is_anomaly'] else "Normal"
            print(f"  * {row['provider']}: SageMaker RCF Score = {row['anomaly_score']:.4f} | Status = {status}")
    else:
        print("  (Known forensic targets not found in predictions)")

    # 9. Save consolidated predictions locally
    output_predictions = "sagemaker_rcf_predictions.parquet"
    df_results.to_parquet(output_predictions)
    print(f"\nSuccessfully saved RCF consolidated predictions to '{output_predictions}'!")
    print("=" * 60)

if __name__ == "__main__":
    main()
