# CODE ARCHITECTURE PARA SA AI UTAL NG SYSTEM (RANDOM FOREST & MCDA)

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor

class WaitTimePredictorAI:
    # Below this many real completed bookings (with a full snapshot - see
    # ai_retrain.py), real samples are too few relative to noise to trust
    # blending in, so retraining is skipped and the current model is kept.
    MIN_REAL_SAMPLES_TO_RETRAIN = 20
    # From this many real samples on, the synthetic bootstrap is dropped and
    # the model learns from real bookings only.
    REAL_ONLY_MIN_SAMPLES = 200
    # Service durations the synthetic data spans - the real catalog's range
    # (15-180 min) - and the fallback when a prediction gets none.
    SYNTHETIC_SERVICE_MINUTES = [15, 20, 30, 35, 45, 60, 90, 180]
    DEFAULT_SERVICE_MINUTES = 30

    def __init__(self):
        self.last_retrain_sample_count = 0
        self.trained_on = 'synthetic'  # then 'blended' or 'real' after retraining
        X, y = self._synthetic_training_data()
        self.model = self._fit_new_model(X, y)
        print("[AI ENGINE] Random Forest Model for Wait-Time prediction initialized and trained successfully.")

    def _synthetic_training_data(self):
        """
        Gumagawa ng initial historical logs para may paunang laman ang AI model bago ikabit sa live DB.
        Sinasanay nito ang AI na intindihin ang epekto ng pila, bilang ng staff, at ulan. Also used as a
        floor/regularizer blended into real data during retraining (see retrain_with_real_data), so a
        still-small real sample doesn't swing predictions wildly.
        """
        np.random.seed(42)
        n_samples = 300

        # Mga input features na tinukoy sa inyong Feature Engineering
        queue_length = np.random.randint(1, 12, n_samples)          # Bilang ng sasakyan sa pila
        active_staff = np.random.randint(2, 6, n_samples)           # Bilang ng active washers sa shift
        precipitation = np.random.uniform(0.0, 12.0, n_samples)     # Dami ng ulan sa mm (Weather API)
        service_base_time = np.random.choice(self.SYNTHETIC_SERVICE_MINUTES, n_samples)  # Tagal ng serbisyo (minuto)

        # Matematikal na ugnayan para sa Target Variable (Actual Wait Time)
        # Ang ulan (precipitation) ay nagpapatagal sa operasyon ng 2.5 minuto kada mm
        actual_wait_time = (queue_length * service_base_time / active_staff) + (precipitation * 2.5)
        actual_wait_time += np.random.normal(0, 4, n_samples)       # Noise/Variance sa totoong buhay
        actual_wait_time = np.clip(actual_wait_time, 5, 240)        # Siguraduhing walang negative o lampas 4 hours

        X = pd.DataFrame({
            'queue_length': queue_length,
            'active_staff': active_staff,
            'precipitation': precipitation,
            'service_base_time': service_base_time
        })
        y = pd.Series(actual_wait_time)
        return X, y

    def _fit_new_model(self, X, y):
        model = RandomForestRegressor(n_estimators=100, random_state=42)
        model.fit(X, y)
        return model

    def retrain_with_real_data(self, real_rows):
        """real_rows: list of dicts with queue_length, active_staff,
        precipitation, service_base_time, actual_wait_time - see
        ai_retrain.py for how these are pulled from real completed
        bookings. Below REAL_ONLY_MIN_SAMPLES they're blended with the
        synthetic bootstrap set as a floor; from there on the model learns
        from real bookings only. Fits a NEW model, then atomically swaps it
        in (self.model = ..., a single attribute assignment) rather than
        mutating the live model in place - concurrent predict_ewt() calls
        from request threads should never see a model mid-fit.

        Returns True if retraining happened, False if there wasn't enough
        real data yet (the existing model is left untouched)."""
        if len(real_rows) < self.MIN_REAL_SAMPLES_TO_RETRAIN:
            return False

        real_df = pd.DataFrame(real_rows)
        X_real = real_df[['queue_length', 'active_staff', 'precipitation', 'service_base_time']]
        y_real = real_df['actual_wait_time']

        if len(real_rows) >= self.REAL_ONLY_MIN_SAMPLES:
            X_train, y_train, trained_on = X_real, y_real, 'real'
        else:
            X_synthetic, y_synthetic = self._synthetic_training_data()
            X_train = pd.concat([X_synthetic, X_real], ignore_index=True)
            y_train = pd.concat([y_synthetic, y_real], ignore_index=True)
            trained_on = 'blended'

        self.model = self._fit_new_model(X_train, y_train)
        self.last_retrain_sample_count = len(real_rows)
        self.trained_on = trained_on
        print(f"[AI ENGINE] Retrained wait-time model on {len(real_rows)} real completed booking(s)"
              f"{'' if trained_on == 'real' else ' blended with the synthetic baseline'}.")
        return True

    def predict_ewt(self, queue_len, staff_count, rain_mm, service_minutes):
        """
        DFD Process 2.3 - Kinukuha ang kasalukuyang operational data para mag-predict ng Estimated Wait Time.
        service_minutes: the service's real duration (services.base_duration_minutes) -
        the same feature retraining learns from.
        """
        try:
            base_time = float(service_minutes)
        except (TypeError, ValueError):
            base_time = 0
        if base_time <= 0:
            base_time = self.DEFAULT_SERVICE_MINUTES

        # Ihanda ang data row para sa prediction logic ng scikit-learn
        input_features = pd.DataFrame([{
            'queue_length': queue_len,
            'active_staff': staff_count,
            'precipitation': rain_mm,
            'service_base_time': base_time
        }])
        
        predicted_minutes = self.model.predict(input_features)[0]
        return round(max(5.0, predicted_minutes), 2) # Hindi pwedeng bumaba sa 5 mins ang wait time


class StaffMCDAEngine:
    def __init__(self):
        # Mga itinalagang bigat (Weights) para sa Simple Additive Weighting (SAW) Method
        # Service Speed (40%), Completion Rate (40%), Customer Rating (20%) = 100%
        self.weights = {
            'service_speed': 0.40,
            'completion_rate': 0.40,
            'customer_rating': 0.20
        }
        
    def evaluate_staff(self, staff_data_list):
        """
        DFD Process 4.0 - Kino-compute ang normalized criteria ranking para sa staff productivity matrix.
        """
        df = pd.DataFrame(staff_data_list)
        if df.empty:
            return []
            
        # Siguraduhing tugma ang mga pangalan ng column galing sa frontend / API payload
        if 'completion' in df.columns and 'completion_rate' not in df.columns:
            df['completion_rate'] = df['completion']
        if 'feedback' in df.columns and 'customer_rating' not in df.columns:
            df['customer_rating'] = df['feedback']

        # I-convert sa float para iwas tip error
        df['service_speed'] = pd.to_numeric(df['service_speed'], errors='coerce').fillna(0)
        df['completion_rate'] = pd.to_numeric(df['completion_rate'], errors='coerce').fillna(0)
        df['customer_rating'] = pd.to_numeric(df['customer_rating'], errors='coerce').fillna(0)

        # Hakbang 1: Normalization ng bawat score (Max Normalization)
        max_speed = df['service_speed'].max() if df['service_speed'].max() > 0 else 1
        max_completion = df['completion_rate'].max() if df['completion_rate'].max() > 0 else 1
        
        # Kung ang customer rating ay nasa 5.0 scale, i-normalize base doon o sa max ng listahan
        max_rating = df['customer_rating'].max() if df['customer_rating'].max() > 5 else 5.0
        if max_rating <= 0:
            max_rating = 1.0
        
        df['norm_speed'] = df['service_speed'] / max_speed
        df['norm_completion'] = df['completion_rate'] / max_completion
        df['norm_rating'] = df['customer_rating'] / max_rating
        
        # Hakbang 2: Pag-timbang gamit ang SAW Formula (Score = Criteria * Weight)
        df['saw_score'] = (
            (df['norm_speed'] * self.weights['service_speed']) +
            (df['norm_completion'] * self.weights['completion_rate']) +
            (df['norm_rating'] * self.weights['customer_rating'])
        )
        
        # Ayusin ang pagkakasunod-sunod mula Rank 1 (pinakamataas) pababa
        df['saw_score'] = df['saw_score'].round(4)
        df = df.sort_values(by='saw_score', ascending=False)
        
        # I-map pabalik ang mga kinakailangang keys para sa Frontend Table rendering
        results = []
        for index, row in enumerate(df.to_dict(orient='records')):
            results.append({
                'rank': index + 1,
                'name': row.get('name', 'Staff'),
                'service_speed': row.get('service_speed'),
                'completion': row.get('completion_rate'),
                'feedback': row.get('customer_rating'),
                'saw_score': row.get('saw_score')
            })
            
        return results