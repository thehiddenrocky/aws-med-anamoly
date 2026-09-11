import os
import pandas as pd
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler
import joblib

def main():
    print("=" * 60)
    print("Medicare Fraud Detection: Downstream ML Training Pipeline")
    print("=" * 60)

    # 1. Define paths
    input_parquet = "recovered_features.parquet"
    output_predictions_parquet = "anomaly_predictions.parquet"
    model_path = "model.joblib"
    scaler_path = "scaler.joblib"

    if not os.path.exists(input_parquet):
        print(f"Error: Could not find input features file at '{input_parquet}'.")
        print("Please ensure you have generated or recovered the features parquet.")
        return

    # 2. Load the Gold layer feature store
    print(f"Loading feature store from: {input_parquet} ...")
    df = pd.read_parquet(input_parquet)
    print(f"Successfully loaded {len(df)} provider profiles.")

    # 3. Define engineered features to ingest into Isolation Forest
    ml_features = [
        'total_claims', 
        'unique_patients', 
        'avg_length_of_stay', 
        'avg_daily_reimbursement', 
        'max_daily_reimbursement', 
        'claims_per_patient_ratio', 
        'high_daily_claims_ratio'
    ]

    print(f"\nSelected {len(ml_features)} features for training:")
    for i, feature in enumerate(ml_features, 1):
        print(f"  {i}. {feature}")

    # Extract training matrix
    X = df[ml_features].fillna(0)

    # 4. Standardize/scale features
    print("\nScaling features using StandardScaler...")
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # 5. Initialize and train Isolation Forest
    contamination_rate = 0.05
    print(f"Initializing Isolation Forest with contamination={contamination_rate} (expecting ~5% anomalies)...")
    model = IsolationForest(
        n_estimators=200,
        contamination=contamination_rate,
        random_state=42,
        n_jobs=-1
    )

    print("Fitting model...")
    model.fit(X_scaled)

    # 6. Generate anomaly scores and predictions
    # score_samples returns negative anomaly scores (lower means more anomalous)
    df['anomaly_score'] = model.score_samples(X_scaled)
    # predict returns 1 for inliers, -1 for outliers/anomalies
    predictions = model.predict(X_scaled)
    df['is_anomaly'] = np.where(predictions == -1, True, False)

    anomalous_count = df['is_anomaly'].sum()
    print(f"\nTraining Complete!")
    print(f"Total Providers Processed: {len(df)}")
    print(f"Flagged Anomalous Providers: {anomalous_count} ({anomalous_count / len(df) * 100:.2f}%)")

    # 7. Identify top anomalous providers
    print("\nTop 10 Most Anomalous Providers (Sorted by Anomaly Score):")
    top_anomalies = df[df['is_anomaly'] == True].sort_values(by='anomaly_score').head(10)
    
    headers = [
        "Provider ID", "Total Claims", "Unique Patients", 
        "Avg Daily Reimb.", "Max Daily Reimb.", "Claims/Patient", "High-Cost Ratio", "Score"
    ]
    print("-" * 125)
    print(f"{headers[0]:<15} | {headers[1]:<12} | {headers[2]:<15} | {headers[3]:<16} | {headers[4]:<16} | {headers[5]:<14} | {headers[6]:<15} | {headers[7]:<8}")
    print("-" * 125)
    for _, row in top_anomalies.iterrows():
        print(
            f"{row['provider']:<15} | "
            f"{int(row['total_claims']):<12} | "
            f"{int(row['unique_patients']):<15} | "
            f"${row['avg_daily_reimbursement']:<15.2f} | "
            f"${row['max_daily_reimbursement']:<15.2f} | "
            f"{row['claims_per_patient_ratio']:<14.4f} | "
            f"{row['high_daily_claims_ratio']:<15.4f} | "
            f"{row['anomaly_score']:<8.4f}"
        )
    print("-" * 125)

    # 8. Check specific known forensic anomalies (reproduced from Athena guide)
    known_targets = ['PRV53033', 'PRV52627', 'PRV53471']
    print(f"\nAudit of Known Forensic Targets:")
    audit_df = df[df['provider'].isin(known_targets)]
    if not audit_df.empty:
        for _, row in audit_df.iterrows():
            status = "ANOMALY (FLAGGED)" if row['is_anomaly'] else "Normal"
            print(f"  * {row['provider']}: Anomaly Score = {row['anomaly_score']:.4f} | Status = {status}")
    else:
        print("  (Known forensic targets not found in the dataset)")

    # 9. Save model, scaler, and predictions
    print(f"\nSaving trained scaler to '{scaler_path}'...")
    joblib.dump(scaler, scaler_path)

    print(f"Saving trained Isolation Forest model to '{model_path}'...")
    joblib.dump(model, model_path)

    print(f"Saving final predictions with anomaly flags to '{output_predictions_parquet}'...")
    df.to_parquet(output_predictions_parquet)
    print("Successfully exported predictions parquet!")
    print("=" * 60)

if __name__ == "__main__":
    main()
