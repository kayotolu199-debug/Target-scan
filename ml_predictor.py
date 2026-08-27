import joblib
import pandas as pd
import numpy as np

class ReversalPredictor:
    def __init__(self, model_path="reversal_model_5day.pkl"):
        """Load the trained model"""
        try:
            self.model = joblib.load(model_path)
            print(f"✅ ML Model loaded from {model_path}")
        except Exception as e:
            print(f"❌ Failed to load model: {e}")
            self.model = None
        
        # These are the EXACT features the model was trained on
        # Order matters! Must match training exactly.
        self.expected_features = [
            'mom_pct', 'avg_vol_10d', 'vol_14d', 'vol_21d', 'pattern_B',
            'sector_Consumer Cyclical', 'sector_Energy', 'sector_Financial Services',
            'sector_Healthcare', 'sector_Industrials', 'sector_Technology'
        ]
    
    def predict_match(self, match_dict):
        """
        Takes a match from your scanner and returns confidence score.
        
        match_dict should have keys:
        - 'pattern': 'A' or 'B'
        - 'sector': e.g., 'Technology', 'Healthcare', etc.
        - 'mom_pct': float (momentum percentage)
        - 'avg_vol_10d': int (10-day average volume)
        - 'vol_14d': 0 or 1
        - 'vol_21d': 0 or 1
        """
        if self.model is None:
            return {
                'confidence': 0.0,
                'recommendation': '⚠️ ML MODEL NOT LOADED',
                'features_used': None
            }
        
        try:
            # Convert match to the exact feature format the model expects
            features = self._preprocess_match(match_dict)
            
            # Get probability (not just 0/1 prediction)
            probability = self.model.predict_proba(features)[0]
            
            # probability[0] = chance of failure, probability[1] = chance of success
            confidence = probability[1] * 100
            
            return {
                'confidence': round(confidence, 1),
                'recommendation': self._get_recommendation(confidence),
                'features_used': features
            }
            
        except Exception as e:
            return {
                'confidence': 0.0,
                'recommendation': f'❌ ERROR: {str(e)[:50]}',
                'features_used': None
            }
    
    def _preprocess_match(self, match):
        """Convert raw match data to model features"""
        # Start with zeros for all features
        feature_values = {
            'mom_pct': match.get('mom_pct', 0),
            'avg_vol_10d': match.get('avg_vol_10d', 0),
            'vol_14d': match.get('vol_14d', 0),
            'vol_21d': match.get('vol_21d', 0),
            'pattern_B': 1 if match.get('pattern') == 'B' else 0,
            'sector_Consumer Cyclical': 0,
            'sector_Energy': 0,
            'sector_Financial Services': 0,
            'sector_Healthcare': 0,
            'sector_Industrials': 0,
            'sector_Technology': 0,
        }
        
        # Set the correct sector to 1 (one-hot encoding)
        sector = match.get('sector', 'Unknown')
        sector_key = f'sector_{sector}'
        if sector_key in feature_values:
            feature_values[sector_key] = 1
        
        # Convert to DataFrame with correct column order
        df = pd.DataFrame([feature_values])[self.expected_features]
        return df
    
    def _get_recommendation(self, confidence):
        """Translate confidence score to trading recommendation"""
        if confidence >= 70:
            return "🟢 STRONG BUY (High confidence)"
        elif confidence >= 55:
            return "🟡 BUY (Moderate confidence)"
        elif confidence >= 40:
            return "🟠 WATCH (Borderline)"
        else:
            return "🔴 SKIP (Low confidence)"
    
    def score_multiple_matches(self, matches_list):
        """Score a list of matches and return sorted by confidence"""
        scored = []
        for match in matches_list:
            result = self.predict_match(match)
            scored.append({
                **match,
                'ml_confidence': result['confidence'],
                'ml_recommendation': result['recommendation']
            })
        
        # Sort by confidence (highest first)
        scored.sort(key=lambda x: x['ml_confidence'], reverse=True)
        return scored


# ──────────────────────────────────────────────────────────────
# EXAMPLE USAGE (for testing)
# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Initialize the predictor
    predictor = ReversalPredictor("reversal_model_5day.pkl")
    
    # Example match from your scanner
    example_match = {
        'ticker': 'AAPL',
        'date': '2026-08-28',
        'pattern': 'A',
        'sector': 'Technology',
        'mom_pct': -12.5,
        'avg_vol_10d': 750000,
        'vol_14d': 1,
        'vol_21d': 0,
        'entry_price': 175.50
    }
    
    # Get prediction
    result = predictor.predict_match(example_match)
    
    print(f"\n🎯 Prediction for {example_match['ticker']}:")
    print(f"   Confidence: {result['confidence']}%")
    print(f"   Recommendation: {result['recommendation']}")
