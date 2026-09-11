import os
import pandas as pd

def main():
    print("=" * 60)
    print("SageMaker RCF Data Preparation")
    print("=" * 60)

    input_parquet = "recovered_features.parquet"
    output_csv = "rcf_train.csv"
    output_mapping = "provider_mapping.csv"

    if not os.path.exists(input_parquet):
        print(f"Error: Could not find input features file at '{input_parquet}'.")
        print("Please ensure you have generated or recovered the features parquet.")
        return

    print(f"Loading feature store from: {input_parquet} ...")
    df = pd.read_parquet(input_parquet)
    print(f"Successfully loaded {len(df)} provider profiles.")

    # Define the 7 numerical features for RCF
    ml_features = [
        'total_claims', 
        'unique_patients', 
        'avg_length_of_stay', 
        'avg_daily_reimbursement', 
        'max_daily_reimbursement', 
        'claims_per_patient_ratio', 
        'high_daily_claims_ratio'
    ]

    print("\nExtracting feature matrix (without header and without index) for RCF...")
    X = df[ml_features].fillna(0)
    
    # Save features to CSV (no index, no headers)
    X.to_csv(output_csv, header=False, index=False)
    print(f"Saved training dataset for RCF to '{output_csv}' ({len(X)} rows).")

    # Save provider ID mapping to maintain row association
    df[['provider']].to_csv(output_mapping, index=False)
    print(f"Saved provider mapping to '{output_mapping}' ({len(df)} rows).")
    print("=" * 60)

if __name__ == "__main__":
    main()
