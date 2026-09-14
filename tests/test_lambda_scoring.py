import os
import sys
import json
import unittest
from unittest.mock import patch, MagicMock

# Dynamically import etl-scripts/lambda-scoring.py (since it contains a hyphen in the filename)
import importlib.util
current_dir = os.path.dirname(os.path.abspath(__file__))
module_path = os.path.abspath(os.path.join(current_dir, '../etl-scripts/lambda-scoring.py'))

spec = importlib.util.spec_from_file_location("lambda_scoring", module_path)
lambda_scoring = importlib.util.module_from_spec(spec)
sys.modules["lambda_scoring"] = lambda_scoring
spec.loader.exec_module(lambda_scoring)

class TestLambdaScoring(unittest.TestCase):
    
    def setUp(self):
        # Set up standard mock credentials so tests don't fail credentials checks
        os.environ['AWS_ACCESS_KEY_ID'] = 'mock-access-key'
        os.environ['AWS_SECRET_ACCESS_KEY'] = 'mock-secret-key'
        os.environ['AWS_SESSION_TOKEN'] = 'mock-session-token'
        os.environ['AWS_REGION'] = 'us-east-1'
        
        # Mock features representing standard provider metrics
        self.mock_features = {
            "provider": "PRV55912",
            "total_claims": 150,
            "unique_patients": 45,
            "avg_length_of_stay": 2.5,
            "avg_daily_reimbursement": 1200.0,
            "max_daily_reimbursement": 5000.0,
            "claims_per_patient_ratio": 3.33,
            "high_daily_claims_ratio": 0.02
        }

    def test_options_preflight(self):
        """
        Verify that OPTIONS requests return 200 with standard CORS headers.
        """
        event = {
            "httpMethod": "OPTIONS",
            "body": ""
        }
        response = lambda_scoring.lambda_handler(event, None)
        self.assertEqual(response["statusCode"], 200)
        self.assertIn("Access-Control-Allow-Origin", response["headers"])
        
        body = json.loads(response["body"])
        self.assertEqual(body["message"], "Success")

    def test_invalid_json_payload(self):
        """
        Verify that malformed JSON payloads return 400 Bad Request.
        """
        event = {
            "httpMethod": "POST",
            "body": "{invalid-json"
        }
        response = lambda_scoring.lambda_handler(event, None)
        self.assertEqual(response["statusCode"], 400)
        
        body = json.loads(response["body"])
        self.assertIn("error", body)
        self.assertIn("Invalid JSON payload", body["error"])

    @patch('lambda_scoring.check_dynamodb_cache')
    def test_missing_features_payload(self, mock_cache):
        """
        Verify that requests missing required features return 400 Bad Request with details.
        """
        mock_cache.return_value = None  # Force cache miss so we proceed to validation
        bad_features = self.mock_features.copy()
        del bad_features["total_claims"]  # Remove a required feature
        
        event = {
            "httpMethod": "POST",
            "body": json.dumps(bad_features)
        }
        response = lambda_scoring.lambda_handler(event, None)
        self.assertEqual(response["statusCode"], 400)
        
        body = json.loads(response["body"])
        self.assertIn("error", body)
        self.assertIn("Missing features", body["error"])

    @patch('lambda_scoring.check_dynamodb_cache')
    def test_cache_hit_serves_instantly(self, mock_cache):
        """
        Verify that if the record is found in DynamoDB cache, it serves it immediately
        without triggering ML model loading or inference.
        """
        cached_data = {
            "provider": "PRV55912",
            "anomaly_score": -0.450000,
            "is_anomaly": False,
            "risk_level": "LOW",
            "genai_analysis": "N/A",
            "features": self.mock_features
        }
        mock_cache.return_value = cached_data
        
        event = {
            "httpMethod": "POST",
            "body": json.dumps(self.mock_features)
        }
        
        # If cache hit works, load_artifacts and scikit-learn models will NOT be called/needed
        with patch('lambda_scoring.load_artifacts') as mock_load_artifacts:
            response = lambda_scoring.lambda_handler(event, None)
            
            mock_cache.assert_called_once_with("PRV55912")
            mock_load_artifacts.assert_not_called()
            
            self.assertEqual(response["statusCode"], 200)
            body = json.loads(response["body"])
            self.assertEqual(body["provider"], "PRV55912")
            self.assertEqual(body["risk_level"], "LOW")
            self.assertEqual(body["anomaly_score"], -0.45)

    @patch('lambda_scoring.check_dynamodb_cache')
    @patch('lambda_scoring.save_to_dynamodb_cache')
    @patch('lambda_scoring.load_artifacts')
    def test_cache_miss_runs_ml_inference_low_risk(self, mock_load, mock_save_cache, mock_check_cache):
        """
        Verify that on cache miss, model inference is executed, low risk case avoids Bedrock summary,
        saves to cache, and returns standard results.
        """
        mock_check_cache.return_value = None  # Cache miss
        
        # Mock the warm caches
        mock_scaler = MagicMock()
        mock_scaler.transform.return_value = [[0.1] * 7]  # Mock scaled metrics
        
        mock_model = MagicMock()
        mock_model.score_samples.return_value = [-0.45]   # Normal/Low anomaly score
        mock_model.predict.return_value = [1]            # 1 = Normal
        
        lambda_scoring.SCALER = mock_scaler
        lambda_scoring.MODEL = mock_model
        
        event = {
            "httpMethod": "POST",
            "body": json.dumps(self.mock_features)
        }
        
        with patch('lambda_scoring.invoke_bedrock_analysis') as mock_bedrock:
            response = lambda_scoring.lambda_handler(event, None)
            
            mock_check_cache.assert_called_once_with("PRV55912")
            mock_load.assert_called_once()
            mock_scaler.transform.assert_called_once()
            mock_model.score_samples.assert_called_once()
            mock_model.predict.assert_called_once()
            
            # Bedrock should NOT be called for LOW risk
            mock_bedrock.assert_not_called()
            
            # Result should be written to cache
            mock_save_cache.assert_called_once_with(
                "PRV55912", -0.45, False, "LOW", "N/A", self.mock_features
            )
            
            self.assertEqual(response["statusCode"], 200)
            body = json.loads(response["body"])
            self.assertEqual(body["provider"], "PRV55912")
            self.assertEqual(body["risk_level"], "LOW")
            self.assertFalse(body["is_anomaly"])
            self.assertEqual(body["genai_analysis"], "N/A")

    @patch('lambda_scoring.check_dynamodb_cache')
    @patch('lambda_scoring.save_to_dynamodb_cache')
    @patch('lambda_scoring.invoke_bedrock_analysis')
    @patch('lambda_scoring.load_artifacts')
    def test_cache_miss_runs_ml_inference_high_risk(self, mock_load, mock_bedrock, mock_save_cache, mock_check_cache):
        """
        Verify that high/critical risk inferences invoke Bedrock to generate summaries,
        write results to cache, and return enriched response payload.
        """
        mock_check_cache.return_value = None  # Cache miss
        mock_bedrock.return_value = "Mocked Bedrock Analysis for High Anomaly"
        
        # Mock warm caches
        mock_scaler = MagicMock()
        mock_scaler.transform.return_value = [[0.9] * 7]
        
        mock_model = MagicMock()
        mock_model.score_samples.return_value = [-0.85]   # Critical anomaly score (< -0.70)
        mock_model.predict.return_value = [-1]           # -1 = Outlier/Anomaly
        
        lambda_scoring.SCALER = mock_scaler
        lambda_scoring.MODEL = mock_model
        
        event = {
            "httpMethod": "POST",
            "body": json.dumps(self.mock_features)
        }
        
        response = lambda_scoring.lambda_handler(event, None)
        
        # Bedrock should be called for CRITICAL risk
        mock_bedrock.assert_called_once_with(
            "PRV55912", -0.85, "CRITICAL", self.mock_features
        )
        
        # Result should be written to cache with Bedrock summary
        mock_save_cache.assert_called_once_with(
            "PRV55912", -0.85, True, "CRITICAL", "Mocked Bedrock Analysis for High Anomaly", self.mock_features
        )
        
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["provider"], "PRV55912")
        self.assertEqual(body["risk_level"], "CRITICAL")
        self.assertTrue(body["is_anomaly"])
        self.assertEqual(body["genai_analysis"], "Mocked Bedrock Analysis for High Anomaly")

if __name__ == '__main__':
    unittest.main()
